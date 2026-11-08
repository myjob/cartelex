"""EN-first candidate selection for the HTTP document downloader.

Pure logic, no I/O. Only PDF candidates are selectable.
"""

from pydantic import BaseModel, Field

from pipelines.download.models import (
    CandidateDocument,
    DecisionRequest,
    SelectionStatus,
)

ORIGIN_PRIORITY: tuple[str, ...] = ("eur-lex", "ec.europa.eu", "db-comp")
PDF_FORMAT = "pdf"
ENGLISH = "EN"
GERMAN = "DE"
LANGUAGE_NA = "NA"


class Selection(BaseModel):
    """Outcome of candidate selection for one decision."""

    status: SelectionStatus = Field(description="Selection state of the decision.")
    selected: list[CandidateDocument] = Field(
        default_factory=list, description="Candidates to download."
    )
    not_selected: list[CandidateDocument] = Field(
        default_factory=list, description="Candidates that are not downloaded."
    )
    language_init: str = Field(
        description="'en', the lower-case original language, or 'NA' if undecided."
    )


def _origin_rank(candidate: CandidateDocument) -> int:
    """Rank a candidate's origin; lower is better, unknown origins last.

    Args:
        candidate: Candidate document.

    Returns:
        Index in ORIGIN_PRIORITY, or its length for unknown origins.
    """
    try:
        return ORIGIN_PRIORITY.index(candidate.origin)
    except ValueError:
        return len(ORIGIN_PRIORITY)


def _original_key(candidate: CandidateDocument) -> tuple[int, int, str, str]:
    """Sort key for choosing the original: origin, then DE first, then stable.

    Args:
        candidate: Marked, non-EN candidate.

    Returns:
        Comparable key; the minimum is the preferred candidate.
    """
    return (
        _origin_rank(candidate),
        0 if candidate.language == GERMAN else 1,
        candidate.language or "",
        candidate.document_id,
    )


class CandidateSelector:
    """Applies the EN-first selection rules to a decision's candidates."""

    def select(self, decision: DecisionRequest) -> Selection:
        """Select the candidates to download for a decision.

        Rules: an EN PDF wins (origin priority); else unmarked PDFs are all
        kept (undecided); else one original-language PDF is chosen; no PDF
        yields no_pdf_source.

        Args:
            decision: Decision with its candidate documents.

        Returns:
            The Selection; every input candidate appears exactly once in
            selected or not_selected.

        Raises:
            None.
        """
        pdfs = [c for c in decision.candidates if c.file_format.lower() == PDF_FORMAT]
        others = [c for c in decision.candidates if c not in pdfs]
        if not pdfs:
            return Selection(
                status=SelectionStatus.NO_PDF_SOURCE,
                not_selected=others,
                language_init=LANGUAGE_NA,
            )

        english = [c for c in pdfs if c.language == ENGLISH]
        unmarked = [c for c in pdfs if c.language is None]
        if english:
            chosen = [min(english, key=_origin_rank)]
            status, language_init = SelectionStatus.SELECTED, ENGLISH.lower()
        elif unmarked:
            chosen = unmarked
            status, language_init = SelectionStatus.UNDECIDED, LANGUAGE_NA
        else:
            best = min(pdfs, key=_original_key)
            chosen = [best]
            status = SelectionStatus.SELECTED
            language_init = (best.language or LANGUAGE_NA).lower()

        rest = [c for c in pdfs if all(c is not s for s in chosen)]
        return Selection(
            status=status,
            selected=chosen,
            not_selected=rest + others,
            language_init=language_init,
        )
