"""
Report: where the book actually discusses the cases behind fact2book mismatches.

``BookCrossCheckModule`` judges infringement-classification facts against the
chapter the Russo-Schinkel book files a case under (``book_taxonomy``). Every
numbered book section (``5.2 Territorial exclusivity ...``) has the same
lettered sub-chapters::

    A Introduction                          prose
    B Applicable EU legislation             reference list
    C Related economics texts               reference list
    D Related legal texts                   reference list
    E Commission decisions in chronological order   bare listing of decisions
    F Economic landmark cases -> F.n <case> detailed case analysis

A case named only in an ``E`` listing is filed there without being analysed, so
a ``mismatch`` that rests on such a listing is weak evidence. This reporter
takes every ``mismatch`` row of ``celex_facts.fact2book_checks`` for the
selected facts, finds every book chunk naming the case (the same case-id
surface resolution the cross-check uses), classifies each mention by its
sub-chapter, and sorts the cases into:

``listing_only``
    The book never discusses the case: only ``E`` listings (and reference
    lists). The mismatch rests purely on where a list places it.
``detailed_elsewhere``
    The chapter the verdict rests on only lists the case, but another chapter
    discusses it (introduction prose or a landmark analysis).
``detailed_in_filing_chapter``
    The chapter the verdict rests on discusses the case in detail -- the
    mismatch is backed by real analysis and deserves a close reading.

The report is spliced into a markdown document between two marker comments so
the hand-written design notes around it survive a re-run.

Exports:
    MentionKind -- sub-chapter kind of one book mention.
    DiscrepancyClass -- per-case classification.
    BookMention, MismatchFact, CaseDiscrepancy -- report records.
    classify_mention -- sub-chapter kind of a book path.
    decision_number -- Commission decision number (``78/155/``) of a CELEX id.
    is_own_landmark -- whether a landmark chunk is the case's own F.n write-up.
    assess_case -- build one CaseDiscrepancy from mismatch rows and mentions.
    render_report -- markdown block for a list of CaseDiscrepancy.
    splice_report -- replace the marker block of a document.
    Fact2BookCaseDiscrepancyReporter -- DB-backed builder.

Usage:
    PYTHONPATH=src .venv/bin/python \\
        src/reporting/utils/fact2book_case_discrepancy_reporter.py

    # Other facts, other output:
    PYTHONPATH=src .venv/bin/python \\
        src/reporting/utils/fact2book_case_discrepancy_reporter.py \\
        --fact-pattern 'infringement_category%' --out /tmp/report.md
"""

import argparse
import logging
import re
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from config.settings import PROJECT_ROOT, settings
from pipelines.agent_dspy.book_cross_check_module import case_id_variants
from pipelines.agent_dspy.book_taxonomy import (
    BookTaxonomyRule,
    book_placement,
    claimed_numbers,
    load_taxonomy_rules,
)
from pipelines.rag.pgvector.book_chunk_repository import BookChunkRepository
from pipelines.rag.pgvector.fact2book_check_repository import (
    Fact2BookCheckRepository,
)

logger = logging.getLogger(__name__)

DEFAULT_FACT_PATTERN = "%infringement%"
DEFAULT_OUT = PROJECT_ROOT / "docs" / "fact2book_case_discrepancy.md"
REPORT_START = "<!-- fact2book-discrepancy-report:start -->"
REPORT_END = "<!-- fact2book-discrepancy-report:end -->"

_PATH_SEPARATOR = " -> "
_EXCERPT_CHARS = 260
_CELEX_DECISION = re.compile(r"^(?:CELEX-)?3(\d{4})D(\d{4})$")
# Decisions carry a two-digit year until 1998 (78/155/EEC) and four from 1999.
_FOUR_DIGIT_YEAR_FROM = 1999
_SUBCHAPTER_LETTER = re.compile(r"^[A-Z]\s")
# A landmark "F.n <case> Commission decision NN/NNN" heading is short and ends
# where the "Relevance:" paragraph begins.
_LANDMARK_HEADING_END = "Relevance"
_LANDMARK_HEADING_CHARS = 200


class MentionKind(StrEnum):
    """Sub-chapter kind of one book mention of a case."""

    LANDMARK = "landmark"
    INTRODUCTION = "introduction"
    BODY = "body"
    REFERENCE = "reference"
    LISTING = "listing"
    INDEX = "index"


# Kinds where the book writes about the case rather than merely naming it.
DETAILED_KINDS: frozenset[MentionKind] = frozenset(
    {MentionKind.LANDMARK, MentionKind.INTRODUCTION, MentionKind.BODY}
)


class DiscrepancyClass(StrEnum):
    """How much the book says about a mismatched case, and where."""

    LISTING_ONLY = "listing_only"
    DETAILED_ELSEWHERE = "detailed_elsewhere"
    DETAILED_IN_FILING_CHAPTER = "detailed_in_filing_chapter"


class BookMention(BaseModel):
    """One book section naming the case."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(description="Book section path of the chunk naming the case.")
    kind: MentionKind = Field(description="Sub-chapter kind of the path.")
    chapter: str | None = Field(default=None, description="Chapter number, e.g. '5'.")
    section: str | None = Field(default=None, description="Section number, e.g. '5.2'.")
    own_landmark: bool = Field(
        default=False,
        description="Landmark heading carries this case's own decision number.",
    )
    excerpt: str = Field(default="", description="Text window around the case id.")


class MismatchFact(BaseModel):
    """One mismatched fact of a case, as recorded in fact2book_checks."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Fact name.")
    value: str = Field(description="Fact value (markdown or JSON text).")
    claimed: list[str] = Field(
        default_factory=list,
        description="Book chapter/section numbers the value claims (taxonomy rule).",
    )
    reference_type: str | None = Field(
        default=None, description="landmark / simple, as recorded by the check."
    )
    explanation: str = Field(description="Recorded verdict explanation.")


class CaseDiscrepancy(BaseModel):
    """Everything the report says about one mismatched case."""

    celex_id: str = Field(description="CELEX document identifier.")
    case_id: str = Field(description="Canonical case identifier (IV/...).")
    book_surface: str | None = Field(
        default=None, description="Case-id spelling found in the book, None if absent."
    )
    facts: list[MismatchFact] = Field(description="Mismatched facts of the case.")
    mentions: list[BookMention] = Field(
        default_factory=list, description="One entry per book section naming the case."
    )
    filing_chapters: list[str] = Field(
        default_factory=list,
        description="Chapters the verdict rests on: placement minus claimed chapters.",
    )
    claimed_chapters: list[str] = Field(
        default_factory=list, description="Chapters the mismatched facts claim."
    )
    classification: DiscrepancyClass | None = Field(
        default=None, description="None when the case id no longer resolves in the book."
    )

    def detailed(self, *, in_filing: bool) -> list[BookMention]:
        """Return detailed mentions inside or outside the filing chapters.

        Parameters
        ----------
        in_filing:
            ``True`` for mentions in a filing chapter, ``False`` for the rest.

        Returns
        -------
        list[BookMention]
            Mentions whose kind is in :data:`DETAILED_KINDS`.
        """
        return [
            m
            for m in self.mentions
            if m.kind in DETAILED_KINDS
            and (m.chapter in self.filing_chapters) == in_filing
        ]

    def filing_kinds(self) -> list[MentionKind]:
        """Return the distinct mention kinds found in the filing chapters.

        Returns
        -------
        list[MentionKind]
            Sorted by enum value.
        """
        return sorted(
            {m.kind for m in self.mentions if m.chapter in self.filing_chapters}
        )


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def classify_mention(path: str) -> MentionKind:
    """Return the sub-chapter kind of a book path.

    Parameters
    ----------
    path:
        Book section path, segments joined by ``" -> "``.

    Returns
    -------
    MentionKind
        ``INDEX`` for back-matter, else the kind of the first lettered
        sub-chapter segment (landmark ``F``, listing ``E``, introduction ``A``,
        any other letter a reference list), else ``BODY`` for chapter or
        section prose above the lettered sub-chapters.
    """
    if path.startswith(settings.BOOK_INDEX_PATH_PREFIXES):
        return MentionKind.INDEX
    for segment in path.split(_PATH_SEPARATOR)[1:]:
        segment = segment.strip()
        if segment.startswith(settings.BOOK_LANDMARK_SECTION_MARKERS):
            return MentionKind.LANDMARK
        if segment.startswith(settings.BOOK_LISTING_SECTION_MARKERS):
            return MentionKind.LISTING
        if segment.startswith(settings.BOOK_INTRODUCTION_SECTION_MARKERS):
            return MentionKind.INTRODUCTION
        if _SUBCHAPTER_LETTER.match(segment):
            return MentionKind.REFERENCE
    return MentionKind.BODY


def decision_number(celex_id: str) -> str | None:
    """Return the Commission decision number a CELEX id encodes.

    Parameters
    ----------
    celex_id:
        CELEX identifier, with or without the ``CELEX-`` prefix.

    Returns
    -------
    str | None
        ``"78/155/"`` for ``CELEX-31978D0155``, ``"2001/478/"`` for
        ``32001D0478``; ``None`` for ids that are not decisions (AT_ / numeric
        case ids).
    """
    match = _CELEX_DECISION.match(celex_id)
    if match is None:
        return None
    year, number = int(match.group(1)), int(match.group(2))
    year_text = str(year) if year >= _FOUR_DIGIT_YEAR_FROM else f"{year % 100:02d}"
    return f"{year_text}/{number}/"


def excerpt(document: str, surface: str, budget: int = _EXCERPT_CHARS) -> str:
    """Return a single-line window of *document* centred on *surface*.

    Parameters
    ----------
    document:
        Chunk text.
    surface:
        The case-id spelling to centre on (case-insensitive).
    budget:
        Maximum window length in characters.

    Returns
    -------
    str
        Whitespace-collapsed window, with ``...`` where it was cut.
    """
    flat = " ".join(document.split())
    idx = flat.lower().find(surface.lower())
    start = max(0, idx - budget // 2) if idx >= 0 else 0
    window = flat[start:start + budget]
    prefix = "..." if start > 0 else ""
    suffix = "..." if start + budget < len(flat) else ""
    return f"{prefix}{window}{suffix}"


def is_own_landmark(path: str, document: str, decision: str | None) -> bool:
    """Return whether a landmark chunk is the case's own ``F.n`` write-up.

    A landmark heading reads ``F.1 BMW Belgium ... Commission decision
    78/155/EEC [1978] ...``. The chunker does not always split below the
    ``F Economic landmark cases`` heading, so the ``F.n`` heading is searched in
    the path and in the chunk text; it must carry the decision number before
    the ``Relevance:`` paragraph or the next ``F.n`` heading starts, so a
    landmark that merely cites the decision does not count.

    Parameters
    ----------
    path:
        Book section path of the chunk.
    document:
        Chunk text.
    decision:
        The case's decision number (:func:`decision_number`), or ``None``.

    Returns
    -------
    bool
        ``True`` when an ``F.n`` heading names *decision*.
    """
    if decision is None:
        return False
    flat = f"{path}{_PATH_SEPARATOR}{' '.join(document.split())}"
    heading = (
        rf"F\.\d+\s(?:(?!{_LANDMARK_HEADING_END}|F\.\d).){{0,{_LANDMARK_HEADING_CHARS}}}?"
        rf"[Dd]ecision (?<!\d){re.escape(decision)}"
    )
    return re.search(heading, flat) is not None


def _mention(path: str, document: str, surface: str, decision: str | None) -> BookMention:
    """Build one BookMention from a chunk naming the case."""
    placement = book_placement([path])
    kind = classify_mention(path)
    own = kind is MentionKind.LANDMARK and is_own_landmark(path, document, decision)
    return BookMention(
        path=path,
        kind=kind,
        chapter=next(iter(placement.chapters), None),
        section=next(iter(placement.sections), None),
        own_landmark=own,
        excerpt=excerpt(document, surface),
    )


def assess_case(
    celex_id: str,
    case_id: str,
    facts: list[MismatchFact],
    surface: str | None,
    chunks: list[tuple[str, str]],
) -> CaseDiscrepancy:
    """Classify one mismatched case from its book mentions.

    Parameters
    ----------
    celex_id, case_id:
        The case.
    facts:
        Its mismatched facts, ``claimed`` already resolved.
    surface:
        Case-id spelling found in the book, ``None`` when absent.
    chunks:
        ``(path, document)`` of every chunk naming *surface*; the first chunk
        per path is kept.

    Returns
    -------
    CaseDiscrepancy
        With filing chapters derived as the taxonomy verdict does
        (:func:`book_placement`: landmark paths when any exist, else all),
        restricted to ``settings.BOOK_TAXONOMY_DECISIVE_CHAPTERS`` and minus the
        chapters the facts claim.
    """
    if surface is None:
        return CaseDiscrepancy(celex_id=celex_id, case_id=case_id, facts=facts)

    decision = decision_number(celex_id)
    by_path: dict[str, str] = {}
    for path, document in chunks:
        by_path.setdefault(path, document)
    mentions = [_mention(p, d, surface, decision) for p, d in by_path.items()]

    claimed_chapters = sorted(
        {number.split(".")[0] for fact in facts for number in fact.claimed}
    )
    placement = book_placement(list(by_path))
    filing = sorted(
        set(placement.chapters)
        & set(settings.BOOK_TAXONOMY_DECISIVE_CHAPTERS)
        - set(claimed_chapters)
    )

    case = CaseDiscrepancy(
        celex_id=celex_id,
        case_id=case_id,
        book_surface=surface,
        facts=facts,
        mentions=mentions,
        filing_chapters=filing,
        claimed_chapters=claimed_chapters,
    )
    if case.detailed(in_filing=True):
        case.classification = DiscrepancyClass.DETAILED_IN_FILING_CHAPTER
    elif case.detailed(in_filing=False):
        case.classification = DiscrepancyClass.DETAILED_ELSEWHERE
    else:
        case.classification = DiscrepancyClass.LISTING_ONLY
    return case


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def _cell(text: str | None) -> str:
    """Escape a value for a markdown table cell."""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _facts_cell(case: CaseDiscrepancy) -> str:
    """Summarise the mismatched facts of a case as ``name = value`` lines."""
    return "<br>".join(
        f"`{f.name}` = {_cell(f.value)[:60]}" for f in case.facts
    )


def _sections(case: CaseDiscrepancy, kinds: frozenset[MentionKind]) -> str:
    """Distinct section (or chapter) numbers of mentions of the given kinds."""
    numbers = sorted(
        {m.section or m.chapter or "?" for m in case.mentions if m.kind in kinds}
    )
    return ", ".join(numbers) or "-"


def _role(case: CaseDiscrepancy, mention: BookMention) -> str:
    """Return how a mention's chapter relates to the verdict."""
    if mention.chapter in case.filing_chapters:
        return "filing"
    if mention.chapter in case.claimed_chapters:
        return "claimed"
    return "other"


def render_report(cases: list[CaseDiscrepancy], fact_pattern: str) -> str:
    """Render the discrepancy lists as a markdown block (between the markers).

    Parameters
    ----------
    cases:
        Assessed cases.
    fact_pattern:
        The fact-name LIKE pattern the report was built for.

    Returns
    -------
    str
        Markdown including :data:`REPORT_START` / :data:`REPORT_END`.
    """
    counts = {c: 0 for c in DiscrepancyClass}
    for case in cases:
        if case.classification is not None:
            counts[case.classification] += 1
    unresolved = [c for c in cases if c.classification is None]
    main_listing = [
        c
        for c in cases
        if c.classification
        in (DiscrepancyClass.LISTING_ONLY, DiscrepancyClass.DETAILED_ELSEWHERE)
    ]
    detailed = [
        c
        for c in cases
        if c.classification
        in (DiscrepancyClass.DETAILED_IN_FILING_CHAPTER, DiscrepancyClass.DETAILED_ELSEWHERE)
    ]
    n_facts = sum(len(c.facts) for c in cases)

    lines = [
        REPORT_START,
        "",
        f"Generated {date.today().isoformat()} for fact pattern `{fact_pattern}`: "
        f"{n_facts} mismatch rows over {len(cases)} cases.",
        "",
        "| classification | cases |",
        "|---|---|",
        *(f"| `{c.value}` | {counts[c]} |" for c in DiscrepancyClass),
        f"| unresolved (case id not in book) | {len(unresolved)} |",
        "",
        "### List 1 -- not discussed in the filing chapter, only listed",
        "",
        "The chapter the verdict rests on names the case only in lists "
        "(`E` chronological decisions, `B`-`D` references). `detailed elsewhere` "
        "cases reappear in list 2.",
        "",
        "| celex_id | case | mismatched facts | claimed | filed under | "
        "kinds in filing chapter | E listings | class |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for case in main_listing:
        lines.append(
            f"| {case.celex_id} | {case.case_id} | {_facts_cell(case)} | "
            f"{', '.join(case.claimed_chapters) or '-'} | "
            f"{', '.join(case.filing_chapters) or '-'} | "
            f"{', '.join(k.value for k in case.filing_kinds()) or '-'} | "
            f"{_sections(case, frozenset({MentionKind.LISTING}))} | "
            f"`{case.classification.value}` |"
        )

    lines += [
        "",
        "### List 2 -- detailed book content available",
        "",
        "Each row is one detailed mention (landmark analysis, introduction or "
        "section prose). `role`: `filing` = chapter the verdict rests on, "
        "`claimed` = chapter the fact claims, `other` = neither. "
        "`own` = the landmark heading carries this case's decision number "
        "(the case is the landmark), otherwise it is only cited there.",
        "",
        "| celex_id | case | mismatched facts | role | kind | own | section path | excerpt |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for case in detailed:
        for mention in case.mentions:
            if mention.kind not in DETAILED_KINDS:
                continue
            path = _PATH_SEPARATOR.join(mention.path.split(_PATH_SEPARATOR)[1:])
            lines.append(
                f"| {case.celex_id} | {case.case_id} | {_facts_cell(case)} | "
                f"{_role(case, mention)} | {mention.kind.value} | "
                f"{'yes' if mention.own_landmark else '-'} | {_cell(path)} | "
                f"{_cell(mention.excerpt)} |"
            )

    if unresolved:
        lines += [
            "",
            "### Unresolved cases",
            "",
            "No spelling of the case id occurs in the book any more; the "
            "mismatch row predates a book re-ingest or a case-id change.",
            "",
            *(f"- {c.celex_id} {c.case_id}" for c in unresolved),
        ]
    lines += ["", REPORT_END]
    return "\n".join(lines) + "\n"


def splice_report(document: str, block: str) -> str:
    """Replace the marker-delimited report block of *document* with *block*.

    Parameters
    ----------
    document:
        Existing markdown document (may be empty).
    block:
        New block, including both markers (:func:`render_report`).

    Returns
    -------
    str
        *document* with its old block replaced, or *block* appended when the
        document has no complete marker pair.
    """
    start = document.find(REPORT_START)
    end = document.find(REPORT_END, start + 1) if start >= 0 else -1
    if start < 0 or end < 0:
        separator = "\n" if document and not document.endswith("\n") else ""
        return f"{document}{separator}{block}"
    tail = document[end + len(REPORT_END):].lstrip("\n")
    return document[:start] + block + (f"\n{tail}" if tail else "")


# ----------------------------------------------------------------------
# DB-backed builder
# ----------------------------------------------------------------------


class Fact2BookCaseDiscrepancyReporter:
    """Build :class:`CaseDiscrepancy` records from the check ledger and the book.

    Parameters
    ----------
    check_repo:
        Reads the ``mismatch`` rows of ``fact2book_checks``.
    book_repo:
        Resolves the case-id surface and reads the chunks naming it.
    rules:
        Taxonomy rules by fact name; defaults to :func:`load_taxonomy_rules`.
    """

    def __init__(
        self,
        check_repo: Fact2BookCheckRepository,
        book_repo: BookChunkRepository,
        rules: dict[str, BookTaxonomyRule] | None = None,
    ) -> None:
        self.check_repo = check_repo
        self.book_repo = book_repo
        self.rules = rules if rules is not None else load_taxonomy_rules()

    def build(self, fact_pattern: str = DEFAULT_FACT_PATTERN) -> list[CaseDiscrepancy]:
        """Assess every case with a mismatch row for the matching facts.

        Parameters
        ----------
        fact_pattern:
            SQL LIKE pattern on the fact name.

        Returns
        -------
        list[CaseDiscrepancy]
            One per ``(celex_id, case_id)``, in ledger order.
        """
        grouped: dict[tuple[str, str], list[MismatchFact]] = {}
        for row in self.check_repo.list_mismatches(fact_pattern):
            grouped.setdefault((row["celex_id"], row["case_id"]), []).append(
                self._fact(row)
            )
        cases = []
        for (celex_id, case_id), facts in grouped.items():
            surface = self._resolve_surface(case_id)
            chunks = (
                self.book_repo.select_reference_chunks(
                    surface, limit=settings.BOOK_CROSSCHECK_REFERENCE_PATH_LIMIT
                )
                if surface is not None
                else []
            )
            cases.append(assess_case(celex_id, case_id, facts, surface, chunks))
        logger.info("Assessed %d mismatched cases for %s", len(cases), fact_pattern)
        return cases

    def _fact(self, row: dict[str, Any]) -> MismatchFact:
        """Build a MismatchFact from a ledger row, resolving its claimed numbers."""
        value = row["value"] or ""
        rule = self.rules.get(row["name"])
        return MismatchFact(
            name=row["name"],
            value=value,
            claimed=claimed_numbers(rule, value) if rule is not None else [],
            reference_type=row["reference_type"],
            explanation=row["explanation"],
        )

    def _resolve_surface(self, case_id: str) -> str | None:
        """Return the first case-id spelling the book contains (as the cross-check does)."""
        for variant in case_id_variants(case_id):
            if self.book_repo.keyword_exists(variant):
                return variant
        return None


def main() -> None:
    """CLI entry point: build the report and splice it into the output document."""
    parser = argparse.ArgumentParser(
        description="List fact2book mismatches whose book placement rests on a listing."
    )
    parser.add_argument(
        "--fact-pattern",
        default=DEFAULT_FACT_PATTERN,
        help=f"SQL LIKE pattern on the fact name (default {DEFAULT_FACT_PATTERN!r}).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Markdown document to splice the report into (created if absent).",
    )
    args = parser.parse_args()

    check_repo = Fact2BookCheckRepository()
    reporter = Fact2BookCaseDiscrepancyReporter(check_repo, BookChunkRepository())
    block = render_report(reporter.build(args.fact_pattern), args.fact_pattern)
    existing = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
    args.out.write_text(splice_report(existing, block), encoding="utf-8")
    logger.info("Wrote %s", args.out)


if __name__ == "__main__":
    main()
