"""
Cross-check extracted CELEX facts against the infringement book's analysis.

For a case that the Russo-Schinkel book actually discusses, this module judges
every fact extracted from the CELEX decision against the book's chapter on that
case and routes the verdict three ways (see the implementation plan,
``docs/claude/plans/fact2book-mismatch-cross-check.md``):

  * **supports**    -> record book corroboration through the lineage mechanic
                       (a ``book_case`` source + a ``corroborated_by`` link).
  * **contradicts** -> the disagreement.
  * **otherwise**   -> skip.

Every judged fact is persisted to ``celex_facts.fact2book_checks`` with the
routing decision in its ``action`` column (``corroborated`` / ``mismatch`` /
``skipped``), so the table is a complete per-fact check ledger rather than a
contradiction-only one. A corroboration additionally records the lineage link.

Classification facts (``settings.BOOK_CLASSIFICATION_FACT_PREFIXES``) are first
checked against the book's chapter *taxonomy* by the deterministic rules in
:mod:`pipelines.agent_dspy.book_taxonomy`: the book files each case under
numbered infringement chapters, so a fact claiming a mutually-exclusive type
(e.g. a horizontal-cartel label for a case the book analyses under abuse of
dominance) is a contradiction the prose-level support judge cannot see — the
chapter is simply silent on the other type. The taxonomy is evaluated against
every chapter naming the case (landmark chapters when there are any) and decides
both ways: a contradiction is recorded as a mismatch, a match as a corroboration
citing the section path (a category label such as ``abuse_of_dominance: True``
has no verbatim surface in chapter prose). An inconclusive taxonomy falls
through to the support judge, which may then corroborate a classification but
never contradict it.

Retrieval reuses the book toolkit / chunk repository rather than re-deriving
book access; the LLM support judge and claim/snippet helpers are shared with the
document verifier via :mod:`pipelines.agent_dspy.evidence_support` (no private
imports across modules).

Design rules honoured: declarative DSPy signature (Rule 1 — via the shared
``EvidenceSupportSignature``), all orchestration in ``forward``/helpers
(Rule 2), Pydantic structured outputs (Rule 3), Provisioner pattern for the
injected DB clients and the book searcher.

Deliberate scope note: book corroboration is recorded on the cross-check ledger
(``fact2book_checks``) and the lineage (``fact_source_registry`` +
``fact_source_link``). It does NOT write ``fact_evidence`` or
``facts.verification_status`` — that ledger belongs to the document-chunk
verifier and is out of scope (plan "Out of scope"); overwriting it would clobber
the per-fact document verdict.
"""

import logging
import re
from enum import StrEnum
from typing import Any

import dspy
from pydantic import BaseModel, Field

from config.settings import settings
from pipelines.agent_dspy.book_taxonomy import (
    BookPlacement,
    BookTaxonomyRule,
    book_placement,
    judge_taxonomy,
    load_taxonomy_rules,
)
from pipelines.agent_dspy.dspy_protocols import BookSearchToolkit
from pipelines.agent_dspy.evidence_support import (
    EvidenceSupportResult,
    EvidenceSupportSignature,
    fact_statement,
    judge_support,
    quote_in_text,
)
from pipelines.rag.pgvector.book_chunk_repository import BookChunkRepository
from pipelines.rag.pgvector.fact2book_check_repository import (
    Fact2BookCheckRepository,
)
from pipelines.rag.pgvector.fact_repository import FactRepository
from pipelines.rag.pgvector.lineage_repository import LineageRepository

logger = logging.getLogger(__name__)

# Edge / source semantics for a book corroboration link.
_RELATION_CORROBORATED = "corroborated_by"
_SOURCE_TYPE_BOOK = "book_case"
_EXTRACTION_METHOD = "book_cross_check"
_DSPY_MODULE = "BookCrossCheckModule"

# Placeholder fact values that carry no claim to judge.
_EMPTY_VALUES: frozenset[str] = frozenset({"", "na", "n/a", "none", "null", "[]"})

# Skip reason for a case-specific fact (settings.BOOK_CASE_SPECIFIC_FACT_PREFIXES)
# when no book chunk names the case: judging it against the rest of the chapter
# would compare it with another case's date, fine or parties.
_NO_CASE_CONTEXT_REASON = "no book chunk names the case for a case-specific fact"

# A thousands-separator dot sitting between two digits (e.g. the "." in
# "26.699"); a leading marker dot like "AT.39610" has a non-digit on its left
# and is deliberately left untouched.
_THOUSANDS_DOT = re.compile(r"(?<=\d)\.(?=\d)")
# The final four digits of a trailing number, split one + three, so an undotted
# case number can be regrouped with a thousands dot (e.g. "26699" -> "26.699").
_TRAILING_GROUP = re.compile(r"(\d)(\d{3})(?!\d)")


def _prefix_swaps(case_id: str) -> list[str]:
    """Return *case_id* rewritten under every other known case-number prefix.

    Parameters
    ----------
    case_id:
        The case identifier as stored.

    Returns
    -------
    list[str]
        One surface per other prefix in ``settings.BOOK_CASE_ID_PREFIXES``
        (e.g. ``IV/36545`` -> ``COMP/36545``); empty when *case_id* carries none
        of the known prefixes.
    """
    for prefix in settings.BOOK_CASE_ID_PREFIXES:
        if case_id.startswith(prefix):
            number = case_id[len(prefix):]
            return [
                other + number
                for other in settings.BOOK_CASE_ID_PREFIXES
                if other != prefix
            ]
    return []


def case_id_variants(case_id: str) -> list[str]:
    """Yield surface variants of a case identifier, the original first.

    EU competition case numbers are written inconsistently in two ways:

    * a dot as a thousands separator in the numeric component (``IV/26.699``
      versus ``IV/26699``);
    * the era prefix (``IV/36545`` before ~1999, ``COMP/36.545`` after), while
      ``celex_case_id`` stores every case under ``IV/``.

    When the stored identifier and the book's prose disagree a literal ILIKE
    lookup misses, so the resolver tries the dot-stripped and dot-regrouped
    surfaces of the stored prefix first, then of every other prefix
    (``settings.BOOK_CASE_ID_PREFIXES``).

    Parameters
    ----------
    case_id:
        The case identifier as stored (``COMP/IV/AT`` form).

    Returns
    -------
    list[str]
        Distinct candidate surfaces in priority order, always starting with
        ``case_id`` unchanged.
    """
    variants: list[str] = []
    for surface in (case_id, *_prefix_swaps(case_id)):
        stripped = _THOUSANDS_DOT.sub("", surface)
        grouped = _TRAILING_GROUP.sub(r"\1.\2", stripped)
        for variant in (surface, stripped, grouped):
            if variant not in variants:
                variants.append(variant)
    return variants


class CrossCheckAction(StrEnum):
    """The write performed for one fact after judging it against the book."""

    CORROBORATED = "corroborated"
    MISMATCH = "mismatch"
    SKIPPED = "skipped"


class BookAbsence(StrEnum):
    """Why a case produced no verdicts, so the driver can report it precisely.

    - ``NONE``: the case is in the book.
    - ``NO_SURFACE``: no spelling variant of the case id occurs in the corpus.
    - ``NO_ANCHOR``: a variant exists but no chunk row could be retrieved
      for it (a retrieval failure rather than a resolution failure).
    """

    NONE = "none"
    NO_SURFACE = "no_surface"
    NO_ANCHOR = "no_anchor"


class BookReferenceType(StrEnum):
    """How the book references a case, which governs the validation policy.

    - ``LANDMARK``: the case is analysed under an "Economic landmark cases"
      subsection (richest prose) — every fact is validated in full.
    - ``SIMPLE``: the case is named in chapter prose as an example. Infringement
      facts are validated fully; any other fact can only be corroborated (an
      explicit quote), never contradicted.
    - ``INDEX``: the case appears only in index / back-matter listings (bare
      case-id lists) — skipped entirely.
    """

    LANDMARK = "landmark"
    SIMPLE = "simple"
    INDEX = "index"


def classify_book_reference(paths: list[str]) -> BookReferenceType:
    """Classify a case's book reference from the section paths that name it.

    Parameters
    ----------
    paths:
        Distinct ``path`` values of book chunks whose text contains the case id.

    Returns
    -------
    BookReferenceType
        ``LANDMARK`` if any path lies under a landmark subsection (it wins even
        when the case is also referenced elsewhere); else ``INDEX`` when every
        path is a pure index/back-matter listing; else ``SIMPLE`` (the default,
        including an empty input and chronological decision listings).
    """
    if any(
        marker in path
        for path in paths
        for marker in settings.BOOK_LANDMARK_SECTION_MARKERS
    ):
        return BookReferenceType.LANDMARK
    if paths and all(
        path.startswith(settings.BOOK_INDEX_PATH_PREFIXES) for path in paths
    ):
        return BookReferenceType.INDEX
    return BookReferenceType.SIMPLE


class BookFactVerdict(BaseModel):
    """Per-fact outcome of the book cross-check (structured output, Rule 3)."""

    fact_id: str = Field(description="UUID of the celex_facts.facts row.")
    fact_name: str = Field(description="Name of the extracted fact.")
    action: CrossCheckAction = Field(
        description="Routing decision: corroborated, mismatch, or skipped."
    )
    label: str | None = Field(
        default=None, description="Judge support label (supports/contradicts/...)."
    )
    support_score: float | None = Field(
        default=None, description="Judge confidence 0.0-1.0, when judged."
    )
    evidence_quote: str | None = Field(
        default=None, description="Exact supporting quote, when corroborated."
    )
    reason: str | None = Field(
        default=None, description="Judge justification (drives the mismatch row)."
    )


class BookCrossCheckResult(BaseModel):
    """Aggregate outcome of cross-checking one case's facts against the book."""

    celex_id: str = Field(description="CELEX document whose facts were checked.")
    case_id: str = Field(description="COMP/IV/AT case identifier matched in the book.")
    in_book: bool = Field(
        description="Whether the case_id was found in the book corpus."
    )
    absence_reason: BookAbsence = Field(
        default=BookAbsence.NONE,
        description="Why the case is absent (no_surface / no_anchor); none when in book.",
    )
    book_chunk_id: str | None = Field(
        default=None,
        description="pg_embedding_book_qwen.id anchoring the citation, when in book.",
    )
    book_path: str | None = Field(
        default=None, description="Chapter/section path used as human context."
    )
    reference_type: BookReferenceType = Field(
        default=BookReferenceType.SIMPLE,
        description="How the book references the case (landmark/simple/index).",
    )
    verdicts: list[BookFactVerdict] = Field(
        default_factory=list, description="Per-fact verdicts produced for this case."
    )
    corroborated_count: int = Field(default=0, description="Facts corroborated.")
    mismatch_count: int = Field(default=0, description="Facts contradicted.")
    skipped_count: int = Field(
        default=0, description="Facts judged and recorded as skipped."
    )
    already_checked_count: int = Field(
        default=0,
        description="Facts left untouched in skip mode (not judged, nothing written).",
    )


def _window_around(text: str, surface: str, budget: int) -> str:
    """Return up to *budget* characters of *text* containing *surface*.

    The head of the text is returned when *surface* is absent or already
    inside the first *budget* characters; otherwise the window is centred on
    the first (case-insensitive) occurrence.

    Parameters
    ----------
    text:
        The chunk text.
    surface:
        The string the window must contain.
    budget:
        Maximum window length in characters.

    Returns
    -------
    str
        The selected window.
    """
    if len(text) <= budget:
        return text
    idx = text.lower().find(surface.lower())
    if idx < 0 or idx + len(surface) <= budget:
        return text[:budget]
    start = max(0, min(idx - budget // 2, len(text) - budget))
    return text[start:start + budget]


def _has_verifiable_value(fact: Any) -> bool:
    """Return True when a fact carries a non-placeholder value worth judging."""
    for value in (fact.value_md, fact.value_json):
        if value is not None and value.strip().lower() not in _EMPTY_VALUES:
            return True
    return False


class BookCrossCheckModule(dspy.Module):
    """Judge a case's CELEX facts against the book chapter and route verdicts.

    Provisioner pattern — the book searcher, DB repositories and the support
    judge are injected so the module is unit-testable with fakes at the DB and
    LLM boundaries.

    Parameters
    ----------
    book_searcher:
        ``BookSearchToolkit`` (the ``HybridBookSearchToolkit``) providing
        ``keyword_exists`` (the "is this case in the book?" gate) and
        ``search_keyword`` (the case's chapter, expanded to full text).
    book_chunk_repo:
        ``BookChunkRepository`` used solely to resolve the keyword-hit
        ``ChunkVo.id`` — the citable ``pg_embedding_book_qwen.id`` anchor that
        the chapter VO does not carry (see ISSUES.md citation-anchor item).
    lineage_repo:
        ``LineageRepository`` for registering the book source and the
        ``corroborated_by`` link on a supporting verdict.
    check_repo:
        ``Fact2BookCheckRepository`` for persisting every per-fact check verdict
        (corroborated / mismatch / skipped) to ``fact2book_checks``.
    fact_repo:
        ``FactRepository`` for loading every fact of the CELEX document.
    support_judge:
        Optional predictor implementing ``EvidenceSupportSignature``. Defaults
        to ``dspy.Predict(EvidenceSupportSignature)``; inject a fake in tests.
    ranker:
        Optional reranker exposing ``rank_chunkvo(query, chunks, top_k)`` (the
        project ``FlashRanker``). When provided, each fact is judged against the
        case chunk that best matches its extraction question (per-fact context +
        per-fact citation anchor). When ``None`` the module falls back to the
        single case-chapter context for every fact. Not defaulted to
        ``FlashRanker()`` because its constructor loads an ONNX model; the batch
        driver constructs one and injects it.
    taxonomy_rules:
        Optional book-taxonomy rules keyed by fact name (see
        :mod:`pipelines.agent_dspy.book_taxonomy`). Defaults to
        :func:`load_taxonomy_rules` (built from settings). Classification facts
        with a rule are decided deterministically against where the book files
        the case, detecting taxonomy contradictions the support judge cannot see.
    overwrite:
        When ``True`` (default), every judged fact is upserted into
        ``fact2book_checks`` so re-runs heal stale verdicts. When ``False``,
        facts that already have a row for the CELEX document are left untouched
        and not re-judged (no LLM call, no write) — an incremental top-up run.
    fact_prefixes:
        Optional tuple of fact-name prefixes; when set, only facts whose
        ``name`` starts with one of them are checked (the rest are ignored).
        Empty / ``None`` checks every fact.
    """

    def __init__(
        self,
        book_searcher: BookSearchToolkit,
        book_chunk_repo: BookChunkRepository,
        lineage_repo: LineageRepository,
        check_repo: Fact2BookCheckRepository,
        fact_repo: FactRepository,
        support_judge: Any | None = None,
        ranker: Any | None = None,
        taxonomy_rules: dict[str, BookTaxonomyRule] | None = None,
        overwrite: bool = True,
        fact_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__()
        self.book_searcher = book_searcher
        self.book_chunk_repo = book_chunk_repo
        self.lineage_repo = lineage_repo
        self.check_repo = check_repo
        self.fact_repo = fact_repo
        self.support_judge = support_judge or dspy.Predict(EvidenceSupportSignature)
        self.ranker = ranker
        self.taxonomy_rules = (
            taxonomy_rules if taxonomy_rules is not None else load_taxonomy_rules()
        )
        self.overwrite = overwrite
        self.fact_prefixes = tuple(fact_prefixes) if fact_prefixes else ()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def forward(
        self, celex_id: str, case_id: str, case_x: str | None = None
    ) -> BookCrossCheckResult:
        """Cross-check every fact of ``celex_id`` against the book's case chapter.

        Parameters
        ----------
        celex_id:
            CELEX document whose facts are checked.
        case_id:
            COMP/IV/AT case identifier used to locate the book chapter.
        case_x:
            Secondary case surface, retained for display/provenance only.

        Returns
        -------
        BookCrossCheckResult
            The per-fact verdicts and aggregate counts. ``in_book`` is ``False``
            (and no facts are judged) when the case is absent from the book.
        """
        # The stored case_id may differ from the book's surface in punctuation
        # or era prefix (e.g. "IV/26699" vs "IV/26.699", "IV/36545" vs
        # "COMP/36.545"); resolve to the surface
        # the book actually contains before any lookup. The canonical case_id is
        # retained for the result and all persistence.
        book_case_id = self._resolve_book_case_id(case_id)
        if book_case_id is None:
            logger.info("Case %s not present in book corpus — no rows written", case_id)
            return BookCrossCheckResult(
                celex_id=celex_id,
                case_id=case_id,
                in_book=False,
                absence_reason=BookAbsence.NO_SURFACE,
            )

        # Chunks whose text literally names the case: the first is the case-level
        # citation anchor, all of them are the candidate pool for case-specific
        # facts (a chapter covers several cases; only these chunks are about
        # this one).
        case_chunks = self.book_chunk_repo.keyword_search(
            book_case_id, limit=settings.BOOK_CROSSCHECK_CANDIDATE_LIMIT
        )
        if not case_chunks:
            logger.info("No keyword-hit chunk for case %s — no rows written", case_id)
            return BookCrossCheckResult(
                celex_id=celex_id,
                case_id=case_id,
                in_book=False,
                absence_reason=BookAbsence.NO_ANCHOR,
            )

        # Case-level fallback context/anchor, computed once. Used for facts that
        # have no per-fact match (no question, no candidates, or no ranker).
        fallback_chunk_id = case_chunks[0].id
        chapter = self.book_searcher.search_keyword(book_case_id)
        fallback_context = self._chapter_context(chapter) or case_chunks[0].chunk
        book_path = chapter[0].path if chapter else None

        # Classify how the book references this case from its section paths. The
        # reference type governs the per-fact validation policy below.
        reference_paths = self.book_chunk_repo.select_reference_paths(
            book_case_id, limit=settings.BOOK_CROSSCHECK_REFERENCE_PATH_LIMIT
        )
        reference_type = classify_book_reference(reference_paths)
        # Where the book files the case (every chapter naming it, landmark
        # chapters when present) -- the evidence for classification facts.
        placement = book_placement(reference_paths)

        if reference_type is BookReferenceType.INDEX:
            logger.info(
                "Case %s appears only in index/back-matter — no facts judged, "
                "no rows written",
                case_id,
            )
            return BookCrossCheckResult(
                celex_id=celex_id,
                case_id=case_id,
                in_book=True,
                book_chunk_id=fallback_chunk_id,
                book_path=book_path,
                reference_type=reference_type,
            )

        # Wider pool for facts that are not case-specific: every chunk of the
        # chapters that mention the case (citable ChunkVos), reranked per fact.
        chapter_chunks = self.book_chunk_repo.fetch_chapters_for_keyword(
            book_case_id, limit=settings.BOOK_CROSSCHECK_CANDIDATE_LIMIT
        )

        result = BookCrossCheckResult(
            celex_id=celex_id,
            case_id=case_id,
            in_book=True,
            book_chunk_id=fallback_chunk_id,
            book_path=book_path,
            reference_type=reference_type,
        )

        # Skip mode: facts already in the ledger are not re-judged or rewritten.
        already_checked: set[str] = (
            self.check_repo.select_checked_fact_ids(celex_id)
            if not self.overwrite
            else set()
        )

        for fact in self.fact_repo.select_all_facts_for_celex(celex_id):
            if self.fact_prefixes and not fact.name.startswith(self.fact_prefixes):
                logger.debug(
                    "Fact %s filtered out by fact_prefixes %s", fact.name, self.fact_prefixes
                )
                continue
            if not self.overwrite and str(fact.id) in already_checked:
                logger.debug("Fact %s already checked — skipping (no overwrite)", fact.name)
                result.verdicts.append(
                    self._skipped(fact, reason="already checked in fact2book_checks")
                )
                # Counted apart from skipped_count: nothing was judged or written.
                result.already_checked_count += 1
                continue
            # The extraction question is the rubric for both the per-fact rerank
            # and the support judge, so it is resolved once per fact. Facts with
            # no verifiable value are skipped without judging — no lookup needed.
            question = (
                self.fact_repo.select_latest_question(celex_id, fact.name) or ""
                if _has_verifiable_value(fact)
                else ""
            )
            pool = self._candidate_pool(fact, case_chunks, chapter_chunks)
            per_fact = self._book_context_for_fact(fact, pool, question, book_case_id)
            context, book_chunk_id = (
                per_fact
                if per_fact is not None
                else (fallback_context, fallback_chunk_id)
            )
            verdict = self._check_fact(
                fact,
                context,
                book_chunk_id,
                book_path,
                case_id,
                celex_id,
                self._contradiction_allowed(fact, reference_type),
                reference_type,
                placement,
                question,
                book_case_id,
            )
            logger.debug(fact.name)
            result.verdicts.append(verdict)
            match verdict.action:
                case CrossCheckAction.CORROBORATED:
                    result.corroborated_count += 1
                case CrossCheckAction.MISMATCH:
                    result.mismatch_count += 1
                case _:
                    result.skipped_count += 1

        logger.info(
            "Book cross-check %s/%s: %d corroborated, %d mismatched, %d skipped, "
            "%d already checked",
            celex_id, case_id, result.corroborated_count,
            result.mismatch_count, result.skipped_count,
            result.already_checked_count,
        )
        return result

    def _resolve_book_case_id(self, case_id: str) -> str | None:
        """Return the case-id surface the book contains, trying surface variants.

        The stored identifier and the book prose can disagree on the
        thousands-separator dot (``IV/26699`` versus ``IV/26.699``) and on the
        era prefix (``IV/36545`` versus ``COMP/36.545``); each variant from
        :func:`case_id_variants` is probed against the book gate and the first
        present surface wins.

        Parameters
        ----------
        case_id:
            The canonical case identifier as stored.

        Returns
        -------
        str | None
            The variant the book contains (the original when it matches
            directly), or ``None`` when no variant is present in the corpus.
        """
        for variant in case_id_variants(case_id):
            if self.book_searcher.keyword_exists(variant):
                if variant != case_id:
                    logger.info(
                        "Case %s resolved to book surface %s via surface variant",
                        case_id,
                        variant,
                    )
                return variant
        return None

    # ------------------------------------------------------------------
    # Per-fact context selection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_case_specific_fact(fact: Any) -> bool:
        """Return True when the fact's value identifies one specific case.

        These facts (``settings.BOOK_CASE_SPECIFIC_FACT_PREFIXES``: decision
        date, conduct period, fines, parties) are only judged against chunks
        that name the case.
        """
        return fact.name.startswith(settings.BOOK_CASE_SPECIFIC_FACT_PREFIXES)

    def _candidate_pool(
        self, fact: Any, case_chunks: list[Any], chapter_chunks: list[Any]
    ) -> list[Any]:
        """Select the book chunks one fact may be judged against.

        A case-specific fact sees only ``case_chunks`` (the chunks naming the
        case). Any other fact sees the case chunks first and then the rest of
        the case's chapters, deduplicated by chunk id, so the reranker can pick
        analytical prose that does not repeat the case number.

        Parameters
        ----------
        fact:
            The fact under judgement.
        case_chunks:
            Chunks whose text names the case (``keyword_search``).
        chapter_chunks:
            Every chunk of the chapters naming the case
            (``fetch_chapters_for_keyword``).

        Returns
        -------
        list[Any]
            The ordered candidate chunks for *fact*.
        """
        if self._is_case_specific_fact(fact):
            return list(case_chunks)
        seen: set[str] = set()
        pool: list[Any] = []
        for chunk in (*case_chunks, *chapter_chunks):
            key = str(chunk.id)
            if key in seen:
                continue
            seen.add(key)
            pool.append(chunk)
        return pool

    def _book_context_for_fact(
        self,
        fact: Any,
        candidate_chunks: list[Any],
        question: str,
        case_surface: str = "",
    ) -> tuple[str, str] | None:
        """Return ``(context_text, anchor_chunk_id)`` for one fact, case-scoped.

        Reranks the fact's candidate pool (see :meth:`_candidate_pool`) against
        the fact's latest extraction question, so each fact is judged against
        the chunk that actually discusses it rather than a single truncated
        case blob.

        The chosen chunk is returned as **both** the judge context and the
        citation anchor: ``_is_support`` requires the quote to be a substring of
        the judged context, so the anchored chunk's text must be what the judge
        reads (the chapter is deliberately not expanded — sibling rows carry no
        citable id).

        Parameters
        ----------
        fact:
            The fact under judgement (its ``name`` drives the question lookup).
        candidate_chunks:
            The fact's candidate pool (``ChunkVo``), chosen by the caller.
        question:
            The fact's latest extraction question, resolved once per fact by the
            caller; empty when the document has none, in which case the fact
            statement is the rerank query.
        case_surface:
            The book's spelling of the case id. For a case-specific fact the
            context window is centred on its first occurrence, so a chunk
            longer than the character budget still shows the judge the case
            (a head-truncated window can cut the surface off and the
            case-surface guard would then skip the fact unjudged).

        Returns
        -------
        tuple[str, str] | None
            ``(context_text, anchor_chunk_id)`` for the best-matching case
            chunk, or ``None`` when no per-fact context can be resolved (no
            ranker, no verifiable value, no candidates, or empty rerank) so the
            caller falls back to the case-chapter context.
        """
        if self.ranker is None or not _has_verifiable_value(fact) or not candidate_chunks:
            return None

        query = question or fact_statement(fact)
        ranked = self.ranker.rank_chunkvo(query, candidate_chunks, top_k=1)
        if not ranked:
            return None

        best = ranked[0]
        budget = settings.BOOK_CROSSCHECK_CONTEXT_CHARS
        if case_surface and self._is_case_specific_fact(fact):
            return _window_around(best.chunk, case_surface, budget), str(best.id)
        return best.chunk[:budget], str(best.id)

    # ------------------------------------------------------------------
    # Per-fact judging + routing
    # ------------------------------------------------------------------

    def _contradiction_allowed(
        self, fact: Any, reference_type: BookReferenceType
    ) -> bool:
        """Return True when a ``contradicts`` verdict for *fact* may be persisted.

        Landmark references validate every fact in full. Simple (example)
        references only flag contradictions on infringement facts
        (``settings.BOOK_INFRINGEMENT_FACT_PREFIXES``); any other fact can be
        corroborated but not contradicted, because a brief example mention is
        silent about most facts rather than denying them.
        """
        if reference_type is BookReferenceType.LANDMARK:
            return True
        return fact.name.startswith(settings.BOOK_INFRINGEMENT_FACT_PREFIXES)

    def _check_fact(
        self,
        fact: Any,
        context: str,
        book_chunk_id: str,
        book_path: str | None,
        case_id: str,
        celex_id: str,
        contradiction_allowed: bool,
        reference_type: BookReferenceType,
        placement: BookPlacement,
        question: str = "",
        case_reference: str = "",
    ) -> BookFactVerdict:
        """Judge one fact, persist the verdict, and return it.

        Every judged fact is written to ``fact2book_checks`` with its routing
        decision in ``action`` (corroborated / mismatch / skipped), so the
        ledger captures all results, not only contradictions. A corroboration
        additionally records the lineage ``corroborated_by`` link; any other
        verdict retracts a book link left by an earlier run, so the lineage
        never claims a corroboration the ledger no longer records.

        ``contradiction_allowed`` gates whether a ``contradicts`` verdict is
        recorded as a mismatch or downgraded to a skip (see
        :meth:`_contradiction_allowed`). Corroboration is unaffected.
        ``reference_type`` is recorded on every row as the verdict's provenance.
        ``placement`` is where the book files the case, the evidence for
        classification facts. ``question`` is the fact's extraction question,
        passed to the judge as its rubric; ``case_reference`` is the case
        surface the book uses, so the judge knows which case the fact belongs to.
        """
        verdict = self._judge_fact(
            fact, context, placement, contradiction_allowed, question, case_reference
        )
        if verdict.action is CrossCheckAction.CORROBORATED:
            self._record_corroboration(fact, book_chunk_id, case_id, celex_id)
        else:
            self._retract_book_links(fact)
        self._record_check(
            verdict, book_chunk_id, book_path, case_id, celex_id, reference_type
        )
        return verdict

    def _judge_fact(
        self,
        fact: Any,
        context: str,
        placement: BookPlacement,
        contradiction_allowed: bool,
        question: str = "",
        case_reference: str = "",
    ) -> BookFactVerdict:
        """Decide the verdict for one fact without writing anything.

        Returns a :class:`BookFactVerdict` whose ``action`` is the routing
        decision; persistence (ledger + lineage) is the caller's responsibility.
        ``question`` is the fact's extraction question, forwarded to the support
        judge as the rubric that defines the fact (empty when unknown).
        ``case_reference`` is forwarded so the judge treats text about another
        case as insufficient rather than contradicting. ``placement`` is where
        the book files the case, the evidence for classification facts.
        """
        if not _has_verifiable_value(fact):
            return self._skipped(fact, reason="fact has no verifiable value to check")
        # A case-specific fact is only judged against text that names the case;
        # the resolved surface is matched case-insensitively, as the book lookup
        # (ILIKE) resolved it.
        if (
            case_reference
            and self._is_case_specific_fact(fact)
            and not quote_in_text(case_reference, context, ignore_case=True)
        ):
            return self._skipped(fact, reason=_NO_CASE_CONTEXT_REASON)

        # Classification facts: the book files each case under an infringement
        # type, so the section path is the book's own verdict on the fact and the
        # taxonomy check decides both ways. A contradiction (mutually-exclusive
        # type) is invisible to the support judge, which only sees a chapter
        # silent on the other type; a match is equally unquotable, because a
        # boolean claim like "abuse_of_dominance: True" has no verbatim surface in
        # chapter prose and would always fall out as insufficient. An
        # inconclusive taxonomy verdict falls through to the prose judge.
        if self._is_classification_fact(fact):
            taxonomy_verdict = self._taxonomy_verdict(
                fact, placement, contradiction_allowed
            )
            if taxonomy_verdict is not None:
                return taxonomy_verdict
            # Only the deterministic taxonomy may contradict a classification;
            # chapter prose can still corroborate it with an exact quote.
            contradiction_allowed = False

        statement = fact_statement(fact)
        verdict = judge_support(
            self.support_judge,
            statement=statement,
            required_elements=fact.name,
            chunk_text=context,
            question=question,
            case_reference=case_reference,
        )
        if verdict is None:
            return self._skipped(fact, reason="support judge returned no verdict")

        if self._is_support(verdict, context):
            return BookFactVerdict(
                fact_id=str(fact.id),
                fact_name=fact.name,
                action=CrossCheckAction.CORROBORATED,
                label=verdict.label,
                support_score=verdict.support_score,
                evidence_quote=verdict.evidence_quote,
                reason=verdict.reason or None,
            )

        if verdict.label == "contradicts" and contradiction_allowed:
            return BookFactVerdict(
                fact_id=str(fact.id),
                fact_name=fact.name,
                action=CrossCheckAction.MISMATCH,
                label=verdict.label,
                support_score=verdict.support_score,
                reason=verdict.reason or None,
            )

        return self._skipped(fact, verdict)

    def _taxonomy_verdict(
        self,
        fact: Any,
        placement: BookPlacement,
        contradiction_allowed: bool,
    ) -> BookFactVerdict | None:
        """Route a classification fact on the book's chapter taxonomy alone.

        Deterministic: :func:`judge_taxonomy` maps the fact's value labels to
        book chapter / section numbers and compares them with where the book
        files the case. The matching section path *is* the evidence, so it is
        recorded as the verdict's quote on a corroboration — there is no prose
        sentence to cite for a category label.

        Parameters
        ----------
        fact:
            The classification fact under judgement.
        placement:
            Where the book files the case (every chapter naming it).
        contradiction_allowed:
            Whether a ``contradicts`` verdict may be recorded (see
            :meth:`_contradiction_allowed`).

        Returns
        -------
        BookFactVerdict | None
            A ``MISMATCH`` on an allowed contradiction, a ``CORROBORATED``
            verdict on a taxonomy match, or ``None`` when the fact has no rule
            or the taxonomy is inconclusive and the caller should fall through
            to the prose support judge.
        """
        rule = self.taxonomy_rules.get(fact.name)
        if rule is None:
            return None
        value = fact.value_md or fact.value_json or ""
        taxonomy = judge_taxonomy(rule, value, placement)
        if taxonomy is None:
            return None
        if taxonomy.label == "contradicts" and contradiction_allowed:
            return BookFactVerdict(
                fact_id=str(fact.id),
                fact_name=fact.name,
                action=CrossCheckAction.MISMATCH,
                label=taxonomy.label,
                support_score=taxonomy.support_score,
                reason=taxonomy.reason,
            )
        if taxonomy.label == "supports":
            return BookFactVerdict(
                fact_id=str(fact.id),
                fact_name=fact.name,
                action=CrossCheckAction.CORROBORATED,
                label=taxonomy.label,
                support_score=taxonomy.support_score,
                evidence_quote=taxonomy.evidence_quote,
                reason=taxonomy.reason,
            )
        return None

    @staticmethod
    def _is_classification_fact(fact: Any) -> bool:
        """Return True when the fact's value is an infringement classification.

        These facts (``settings.BOOK_CLASSIFICATION_FACT_PREFIXES``) are checked
        against the book's chapter taxonomy rather than chapter prose.
        """
        return fact.name.startswith(settings.BOOK_CLASSIFICATION_FACT_PREFIXES)

    @staticmethod
    def _is_support(verdict: EvidenceSupportResult, context: str) -> bool:
        """Return True for a high-confidence ``supports`` verdict with a real quote.

        The quote must occur in the judged context up to surface folding
        (whitespace, hyphenation, ligatures, typographic punctuation; see
        :func:`quote_in_text`) -- the judge is instructed never to paraphrase --
        and the score must clear ``settings.EVIDENCE_SUPPORT_MIN_SCORE``, the
        same bar the document verifier uses to promote a chunk to
        ``verified_supported``.
        """
        return (
            verdict.label == "supports"
            and quote_in_text(verdict.evidence_quote, context)
            and verdict.support_score >= settings.EVIDENCE_SUPPORT_MIN_SCORE
        )

    def _record_corroboration(
        self, fact: Any, book_chunk_id: str, case_id: str, celex_id: str
    ) -> None:
        """Register the book source and link the fact as ``corroborated_by``.

        The book confirms a fact extracted from the decision, so it is a second,
        corroborating source — not the extraction source. Both calls are
        idempotent (the registry upserts on ``chunk_id``; the link upserts on
        ``(fact_id, source_id)``), so re-runs heal rather than duplicate.

        The citation anchor is re-selected by the reranker on every run, so a
        drifted anchor registers a different source; links to any earlier
        anchor are retracted, leaving exactly one book link per fact.
        """
        source_id = self.lineage_repo.register_book_source(
            celex_id=celex_id,
            chunk_id=book_chunk_id,
            description=f"book chapter for case {case_id}",
        )
        self.lineage_repo.link_fact_to_source(
            fact_id=fact.id,
            source_id=source_id,
            extraction_method=_EXTRACTION_METHOD,
            relation_type=_RELATION_CORROBORATED,
            source_type=_SOURCE_TYPE_BOOK,
            dspy_module=_DSPY_MODULE,
        )
        self._retract_book_links(fact, keep_source_id=source_id)

    def _retract_book_links(self, fact: Any, keep_source_id: Any | None = None) -> None:
        """Remove this module's book ``corroborated_by`` links for *fact*.

        Scoped to ``extraction_method='book_cross_check'`` so ``book_case``
        links written by other producers (the fact ingestor) are untouched.

        Parameters
        ----------
        fact:
            The fact whose book corroboration links are retracted.
        keep_source_id:
            Source to preserve (the current anchor on a corroboration);
            ``None`` retracts every book link of this module for the fact.
        """
        removed = self.lineage_repo.retract_fact_links(
            fact_id=fact.id,
            relation_type=_RELATION_CORROBORATED,
            extraction_method=_EXTRACTION_METHOD,
            source_type=_SOURCE_TYPE_BOOK,
            keep_source_id=keep_source_id,
        )
        if removed:
            logger.info(
                "Retracted %d stale book corroboration link(s) for fact %s",
                removed,
                fact.name,
            )

    def _record_check(
        self,
        verdict: BookFactVerdict,
        book_chunk_id: str,
        book_path: str | None,
        case_id: str,
        celex_id: str,
        reference_type: BookReferenceType,
    ) -> None:
        """Upsert one check row for *verdict* (idempotent on ``(fact_id, celex_id)``).

        The routing decision is stored in ``action`` so the ledger records every
        result (corroborated / mismatch / skipped), not only contradictions.
        """
        self.check_repo.upsert_check(
            fact_id=verdict.fact_id,
            celex_id=celex_id,
            book_chunk_id=book_chunk_id,
            explanation=self._explanation_for(verdict),
            action=verdict.action.value,
            case_id=case_id,
            book_path=book_path,
            support_label=verdict.label,
            final_score=verdict.support_score,
            reference_type=reference_type.value,
        )

    @staticmethod
    def _explanation_for(verdict: BookFactVerdict) -> str:
        """Return a non-empty explanation for a check row (``explanation`` is NOT NULL).

        Prefers the judge reason, then a supporting/contradicting quote, then a
        generic per-action fallback so a verdict that carries neither (e.g. an
        empty-value skip) still satisfies the NOT NULL column.
        """
        if verdict.reason:
            return verdict.reason
        if verdict.evidence_quote:
            return verdict.evidence_quote
        match verdict.action:
            case CrossCheckAction.CORROBORATED:
                return f"Book corroborates fact '{verdict.fact_name}'."
            case CrossCheckAction.MISMATCH:
                return f"Book contradicts fact '{verdict.fact_name}'."
            case _:
                return (
                    f"Book neither supports nor contradicts fact "
                    f"'{verdict.fact_name}'."
                )

    @staticmethod
    def _skipped(
        fact: Any,
        verdict: EvidenceSupportResult | None = None,
        reason: str | None = None,
    ) -> BookFactVerdict:
        """Build a skipped verdict for a fact the book neither supports nor denies.

        ``reason`` overrides the judge reason for skips with no judge verdict
        (an empty fact value, a failed judge call, or an already-checked fact in
        skip mode); it doubles as the recorded row explanation.
        """
        return BookFactVerdict(
            fact_id=str(fact.id),
            fact_name=fact.name,
            action=CrossCheckAction.SKIPPED,
            label=verdict.label if verdict is not None else None,
            support_score=verdict.support_score if verdict is not None else None,
            reason=reason
            or ((verdict.reason or None) if verdict is not None else None),
        )

    # ------------------------------------------------------------------
    # Context assembly
    # ------------------------------------------------------------------

    @staticmethod
    def _chapter_context(chapter: list[Any]) -> str:
        """Concatenate chapter rows (doc_pos order) into a capped judge context.

        ``fetch_chapter_text`` already orders rows by ``doc_pos``; their
        ``document`` text is joined and truncated to
        ``settings.BOOK_CROSSCHECK_CONTEXT_CHARS`` so one chapter maps to a
        single bounded judge prompt rather than dozens of LLM calls.
        """
        budget = settings.BOOK_CROSSCHECK_CONTEXT_CHARS
        parts: list[str] = []
        used = 0
        for row in chapter:
            text = (row.document or "").strip()
            if not text:
                continue
            parts.append(text)
            used += len(text)
            if used >= budget:
                break
        return "\n\n".join(parts)[:budget]
