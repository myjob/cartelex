"""
Deterministic book-taxonomy check for infringement-classification facts.

The Russo-Schinkel book files every case under numbered infringement chapters
(``2 Horizontal restrictions``) and sections (``2.1 Price fixing collusion``),
and the value vocabularies of the classification facts map onto those headings
almost one to one. Comparing the two is therefore a lookup, not an LLM
judgement: the former LLM taxonomy judge produced most of the doubtful
``mismatch`` rows (see ``docs/Design Russo-Schinkel-Book vs Facts
Reconciliation.md``, findings F-2 / F-3).

Rules
-----
* **Placement** -- where the book files a case: the numbered chapters and
  sections of its landmark paths when it has any (the book's definitive
  analysis), otherwise of every path naming the case, so a case discussed in
  several chapters is judged against all of them.
* **Label facts** (category / subcategory): the labels found in the value map to
  chapter or section numbers. ``supports`` when a claimed number is in the
  placement; ``contradicts`` when no claimed *chapter* is in the placement but
  the book files the case under a decisive conduct chapter
  (``settings.BOOK_TAXONOMY_DECISIVE_CHAPTERS``); otherwise inconclusive. A
  different section inside a claimed chapter is inconclusive -- a cartel filed
  under price fixing may still be a market-sharing cartel.
* **Boolean facts**: ``True`` supports and ``False`` contradicts when the fact's
  chapter is in the placement; the book's silence decides nothing.
"""

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from config.settings import settings
from pipelines.agent_dspy.evidence_support import EvidenceSupportResult

_PATH_SEPARATOR = " -> "
_CHAPTER_SEGMENT = 1
_SECTION_SEGMENT = 2
_CHAPTER_NUMBER = re.compile(r"^(\d+)\s")
_SECTION_NUMBER = re.compile(r"^(\d+\.\d+)\s")
_SECTION_DELIMITER = "."
_TRUE_VALUES: frozenset[str] = frozenset({"true", "yes"})
_FALSE_VALUES: frozenset[str] = frozenset({"false", "no"})
_VALUE_QUOTES = "\"'"
# A rule lookup is certain about what it compares; the verdict is not a guess.
_TAXONOMY_SCORE = 1.0


class BookTaxonomyRule(BaseModel):
    """How one classification fact's value maps onto the book's numbered headings."""

    model_config = ConfigDict(frozen=True)

    fact_name: str = Field(description="Classification fact the rule applies to.")
    label_numbers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Lower-case value label -> book chapter ('2') or section ('2.1') "
            "number; set for label-valued facts only."
        ),
    )
    boolean_chapter: str | None = Field(
        default=None,
        description="Chapter number a True value asserts; set for boolean facts only.",
    )

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> "BookTaxonomyRule":
        """Require exactly one of ``label_numbers`` or ``boolean_chapter``.

        Raises
        ------
        ValueError
            When both or neither are set.
        """
        if bool(self.label_numbers) == (self.boolean_chapter is not None):
            raise ValueError(
                f"taxonomy rule for {self.fact_name!r} needs exactly one of "
                "label_numbers or boolean_chapter"
            )
        return self


class BookPlacement(BaseModel):
    """Where the book files a case: numbered chapters and sections, each with a citing path."""

    model_config = ConfigDict(frozen=True)

    chapters: dict[str, str] = Field(
        default_factory=dict,
        description="Chapter number -> first (sorted) book path filing the case there.",
    )
    sections: dict[str, str] = Field(
        default_factory=dict,
        description="Section number -> first (sorted) book path filing the case there.",
    )

    def path_for(self, number: str) -> str | None:
        """Return the citing path for a chapter or section number.

        Parameters
        ----------
        number:
            A chapter (``"2"``) or section (``"2.1"``) number.

        Returns
        -------
        str | None
            The book path filing the case under *number*, or ``None``.
        """
        if _SECTION_DELIMITER in number:
            return self.sections.get(number)
        return self.chapters.get(number)

    def chapter_title(self, chapter: str) -> str:
        """Return the chapter heading (e.g. ``"3 Abuse of dominance"``) for *chapter*.

        Parameters
        ----------
        chapter:
            A chapter number present in :attr:`chapters`.

        Returns
        -------
        str
            The chapter segment of its citing path.

        Raises
        ------
        KeyError
            When *chapter* is not part of the placement.
        """
        return self.chapters[chapter].split(_PATH_SEPARATOR)[_CHAPTER_SEGMENT]


def load_taxonomy_rules() -> dict[str, BookTaxonomyRule]:
    """Build the taxonomy rules from settings, keyed by fact name.

    Returns
    -------
    dict[str, BookTaxonomyRule]
        One rule per fact in ``settings.BOOK_TAXONOMY_LABEL_NUMBERS`` and
        ``settings.BOOK_TAXONOMY_BOOLEAN_CHAPTERS``.

    Raises
    ------
    pydantic.ValidationError
        When a configured rule is malformed.
    """
    rules = {
        name: BookTaxonomyRule(fact_name=name, label_numbers=labels)
        for name, labels in settings.BOOK_TAXONOMY_LABEL_NUMBERS.items()
    }
    rules.update(
        {
            name: BookTaxonomyRule(fact_name=name, boolean_chapter=chapter)
            for name, chapter in settings.BOOK_TAXONOMY_BOOLEAN_CHAPTERS.items()
        }
    )
    return rules


def book_placement(paths: list[str]) -> BookPlacement:
    """Derive where the book files a case from the section paths that name it.

    Parameters
    ----------
    paths:
        Distinct ``path`` values of book chunks whose text contains the case id.

    Returns
    -------
    BookPlacement
        Numbered chapters/sections of the landmark paths when any exist,
        otherwise of all paths. Paths without a numbered chapter (index,
        back-matter) contribute nothing.
    """
    landmark = [
        path
        for path in paths
        if any(marker in path for marker in settings.BOOK_LANDMARK_SECTION_MARKERS)
    ]
    chapters: dict[str, str] = {}
    sections: dict[str, str] = {}
    for path in sorted(landmark or paths):
        segments = path.split(_PATH_SEPARATOR)
        chapter = _heading_number(segments, _CHAPTER_SEGMENT, _CHAPTER_NUMBER)
        if chapter is None:
            continue
        chapters.setdefault(chapter, path)
        section = _heading_number(segments, _SECTION_SEGMENT, _SECTION_NUMBER)
        if section is not None:
            sections.setdefault(section, path)
    return BookPlacement(chapters=chapters, sections=sections)


def judge_taxonomy(
    rule: BookTaxonomyRule, value: str, placement: BookPlacement
) -> EvidenceSupportResult | None:
    """Compare a classification fact value with where the book files the case.

    Parameters
    ----------
    rule:
        The taxonomy rule for the fact.
    value:
        The fact value (markdown or JSON text).
    placement:
        Where the book files the case (:func:`book_placement`).

    Returns
    -------
    EvidenceSupportResult | None
        ``supports`` (quote = the citing book path) or ``contradicts``, both
        with score 1.0; ``None`` when the taxonomy does not decide the fact.
    """
    if rule.boolean_chapter is not None:
        return _judge_boolean(rule, value, placement)
    return _judge_labels(rule, value, placement)


def claimed_numbers(rule: BookTaxonomyRule, value: str) -> list[str]:
    """Return the book chapter / section numbers a fact value claims.

    Parameters
    ----------
    rule:
        The taxonomy rule for the fact.
    value:
        The fact value (markdown or JSON text).

    Returns
    -------
    list[str]
        Sorted distinct numbers: for a label fact every chapter or section
        whose label occurs in the value; for a boolean fact its chapter when the
        value is True, otherwise nothing.
    """
    if rule.boolean_chapter is not None:
        flag = value.strip().strip(_VALUE_QUOTES).lower()
        return [rule.boolean_chapter] if flag in _TRUE_VALUES else []
    text = value.lower()
    return sorted({number for label, number in rule.label_numbers.items() if label in text})


def _judge_labels(
    rule: BookTaxonomyRule, value: str, placement: BookPlacement
) -> EvidenceSupportResult | None:
    """Judge a category / subcategory fact whose value names labels."""
    claimed = claimed_numbers(rule, value)
    if not claimed:
        return None

    for number in claimed:
        path = placement.path_for(number)
        if path is not None:
            return _result(
                "supports",
                f"Book files the case under {number}, matching the claimed value.",
                quote=path,
            )

    claimed_chapters = {number.split(_SECTION_DELIMITER)[0] for number in claimed}
    if claimed_chapters & placement.chapters.keys():
        return None

    decisive = sorted(
        set(settings.BOOK_TAXONOMY_DECISIVE_CHAPTERS) & placement.chapters.keys()
    )
    if not decisive:
        return None
    filed_under = "; ".join(placement.chapter_title(chapter) for chapter in decisive)
    return _result(
        "contradicts",
        f"Book taxonomy files the case under {filed_under}, not under the claimed "
        f"{', '.join(claimed)}.",
    )


def _judge_boolean(
    rule: BookTaxonomyRule, value: str, placement: BookPlacement
) -> EvidenceSupportResult | None:
    """Judge a True/False category fact against its chapter."""
    path = placement.path_for(rule.boolean_chapter)
    if path is None:
        return None
    flag = value.strip().strip(_VALUE_QUOTES).lower()
    title = placement.chapter_title(rule.boolean_chapter)
    if flag in _TRUE_VALUES:
        return _result("supports", f"Book files the case under {title}.", quote=path)
    if flag in _FALSE_VALUES:
        return _result(
            "contradicts", f"Book files the case under {title}, but the fact says False."
        )
    return None


def _heading_number(
    segments: list[str], index: int, pattern: re.Pattern[str]
) -> str | None:
    """Return the heading number at path segment *index*, or ``None``."""
    if len(segments) <= index:
        return None
    match = pattern.match(segments[index])
    return match.group(1) if match else None


def _result(label: str, reason: str, quote: str | None = None) -> EvidenceSupportResult:
    """Build a deterministic taxonomy verdict."""
    return EvidenceSupportResult(
        label=label, support_score=_TAXONOMY_SCORE, evidence_quote=quote, reason=reason
    )
