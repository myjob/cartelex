import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Optional

import dspy
import tiktoken

from config.settings import settings
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.dspy_protocols import (
    SQLFactStore,
    HybridSearcher,
    KeywordSearch,
    RegexRetriever,
    BookKeywordSearch,
    BookSearcher,
    BookSearchToolkit,
    FactOutput,
)
from pipelines.agent_dspy.dspy_signature import (
    GenericExtractionSignature,
    GenericVerificationSignature,
    DetermineSupportingChunksSignature,
    DependentFactExtractionSignature,
    EntityDetailExtractionSignature,
    FactComparisonSignature,
    CaseSummarySignature,
    FactCaseTitleSignature,
)
from extract.rag.ranker.ranker_factory import RankerProtocol
from pipelines.agent_dspy.book_cross_check_module import (
    BookReferenceType,
    case_id_variants,
    classify_book_reference,
)
from pipelines.legal.infringement_concept import concepts_for
from pipelines.nace.nace_tree import NaceTree
from pipelines.rag.pgvector.book_chunk_repository import BookChunkRepository
from tools.PipelineFactIngestor import LineageSourceVO

logger = logging.getLogger(__name__)

_STR_SCHEMA = "value (single complete answer as a plain string — do NOT split into multiple keys or list items)"

_SUPPORTED_OUTPUT_TYPES = frozenset({"dict", "str"})

# Maximum doc_pos references persisted per fact; matches the "max 3" contract
# of DetermineSupportingChunksSignature so lineage stays uniformly tidy.
_MAX_REFERENCES_DOC = 3

# LLM detail values treated as "not found" and normalized to the literal 'NA'.
_NA_SENTINELS = frozenset({"na", "n/a", "none", "null"})


def _log_dspy_history(
    action: str, identifier: str, context: Optional[str] = None, use_info: bool = False
) -> None:
    """Log the last prompt sent to the LLM via DSPy."""
    if dspy.settings.lm and getattr(dspy.settings.lm, "history", None):
        last_prompt = dspy.settings.lm.history[-1]
        log_func = logger.info if use_info else logger.debug

        action_str = f"{action} " if action else ""
        if (
            context is not None
            and getattr(settings, "CELEX_FACTS_PROMPT_DEBUG", "False") == "True"
        ):
            log_func(
                "DSPy %sPrompt for '%s':\n%s\nContext:\n%s",
                action_str,
                identifier,
                last_prompt,
                context,
            )
        else:
            log_func(
                "DSPy %sPrompt used for '%s':\n%s", action_str, identifier, last_prompt
            )


# Lazy-initialised tokeniser (module-level singleton)
_tiktoken_enc: Optional[tiktoken.Encoding] = None


def _get_tiktoken_enc() -> tiktoken.Encoding:
    """Return a cached ``cl100k_base`` tiktoken encoder."""
    global _tiktoken_enc
    if _tiktoken_enc is None:
        _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
    return _tiktoken_enc


def _check_context_token_limit(context: str, identifier: str) -> None:
    """Raise ``ValueError`` when *context* exceeds the configured prompt-token budget.

    Uses tiktoken (``cl100k_base``) for an accurate token count and compares
    against ``settings.OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT``.

    Args:
        context: The assembled text that will be sent to the LLM.
        identifier: A human-readable label (e.g. celex_id or target_fact_name)
            used in log messages.

    Raises:
        ValueError: If the token count exceeds the limit.
    """
    enc = _get_tiktoken_enc()
    token_count = len(enc.encode(context))
    limit = settings.OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT
    if token_count > limit:
        logger.error(
            "Context for DSPy generation '%s' is too long: %d tokens (limit %d).",
            identifier,
            token_count,
            limit,
        )
        raise ValueError(
            f"Context too long {token_count} tokens (limit {limit}) -- breaking the chain"
        )


def _resolve_schema_description(params: "FactExtractorParamVO") -> str:
    """Return the schema_description string to pass to DSPy based on output_type.

    Supported output_type values:
        - ``"dict"`` (default): multi-key structured extraction driven by ``output_schema``.
        - ``"str"``: single free-text answer; DSPy is constrained to one ``{"value": ...}`` entry.

    Args:
        params: The fact extractor parameters for the current run.

    Returns:
        A schema description string that steers DSPy key generation.

    Raises:
        ValueError: If ``params.output_type`` is not a recognised type.
    """
    if params.output_type not in _SUPPORTED_OUTPUT_TYPES:
        raise ValueError(
            f"Unsupported output_type '{params.output_type}' for '{params.target_fact_name}'. "
            f"Supported types: {sorted(_SUPPORTED_OUTPUT_TYPES)}"
        )
    if params.output_type == "str":
        return _STR_SCHEMA
    return params.output_schema if params.output_schema else params.target_fact_name


def _check_fact_exists(
    sql_store: SQLFactStore,
    params: FactExtractorParamVO,
    as_boolean: bool = False,
    source_name: str = "facts",
) -> tuple[Optional[dspy.Prediction], list]:
    """
    Check if a fact already exists in the SQL store for a given celex_id and target_fact_name.
    Returns a tuple of (Prediction, context_facts). If a fast-return is possible, Prediction is set.
    """
    if params.force_override:
        return None, []

    logger.debug(f"Starting SQL lookup for '{params.target_fact_name}'")
    t0 = time.perf_counter()
    facts = sql_store.select_fact4celex(params.celex_id, params.target_fact_name)
    logger.debug(f"SQL lookup took {time.perf_counter() - t0:.4f}s")

    if facts:
        if facts[-1].value_json:
            logger.debug(
                "Found existing JSON array in SQL! Skipping further extraction."
            )
            return dspy.Prediction(
                output=FactOutput(items=facts[-1].value_json, source=source_name)
            ), facts
        if as_boolean and any(f.value_md for f in facts):
            items = [{params.target_fact_name: "True"}]
            return dspy.Prediction(
                output=FactOutput(items=items, source=source_name)
            ), facts

    return None, facts if facts else []


# --- The Universal Module (Rule 2) ---


class UniversalFactModule(dspy.Module):
    def __init__(
        self,
        sql_store: SQLFactStore,
        searcher: HybridSearcher,
        chunk_repo: Optional[Any] = None,
    ):
        super().__init__()
        self.sql_store = sql_store
        self.searcher = searcher
        # Chunk store used for the small-document full-context bypass. Falls back
        # to the searcher's own repo so existing call sites keep working.
        self.chunk_repo = chunk_repo if chunk_repo is not None else getattr(
            searcher, "repo", None
        )

        self.extract = dspy.ChainOfThought(GenericExtractionSignature)
        self.verify = dspy.ChainOfThought(GenericVerificationSignature)
        self.find_doc_references = dspy.ChainOfThought(
            DetermineSupportingChunksSignature
        )

    def _use_full_document(self, params: FactExtractorParamVO) -> bool:
        """Decide whether to feed the entire document instead of retrieving.

        Short decisions embed poorly against loaded questions, so ranking can
        drop the very chunks naming the parties. When the whole document fits
        the small-document budget and the fact is not section-scoped, feeding
        every chunk is both cheaper and more reliable than hybrid retrieval.

        Args:
            params: Extraction parameters for the current fact.

        Returns:
            True when the full-document context should be used.
        """
        if self.chunk_repo is None or params.section_scope:
            return False
        total_tokens = self.chunk_repo.sum_document_tokens(params.celex_id)
        # Only bypass when the repo reports a concrete token count; a store that
        # cannot answer (e.g. absent method) keeps the hybrid retrieval path.
        if not isinstance(total_tokens, int):
            return False
        return 0 < total_tokens <= settings.SMALL_DOC_CONTEXT_TOKEN_LIMIT

    def forward(self, params: FactExtractorParamVO):
        # 1. Provisioning (The Slick Pipe)
        context = ""
        source = ""

        prediction, facts = _check_fact_exists(self.sql_store, params)
        if prediction:
            return prediction

        if facts:
            source = "facts"
            context = "\n".join([f.value_md for f in facts if f.value_md])

        if not context and self._use_full_document(params):
            source = "full_doc"
            logger.debug(
                f"Using full-document context for target_fact '{params.target_fact_name}'"
            )
            rows = self.chunk_repo.select_chunks_for_sections(params.celex_id)
            context = "\n---\n".join(
                [f"[doc_pos: {r['doc_pos']}]\n{r['document']}" for r in rows]
            )

        if not context:
            source = "hybrid"
            logger.debug(
                f"Starting Hybrid lookup for target_fact '{params.target_fact_name}'"
            )
            t0 = time.perf_counter()
            ranked_chunks = self.searcher.search(
                celex_id=params.celex_id,
                query=params.question,
                top_k=settings.HYBRID_SEARCH_TOP_K,
                section_types=params.section_scope,
            )
            logger.debug(f"Hybrid lookup took {time.perf_counter() - t0:.4f}s")
            context = "\n---\n".join(
                [f"[doc_pos: {c.doc_pos}]\n{c.chunk}" for c in ranked_chunks]
            )

        logger.debug(f"Context for '{params.target_fact_name}': {context[:40]}...")

        if not context:
            return dspy.Prediction(output=FactOutput(items=[], source="none"))

        _check_context_token_limit(context, params.target_fact_name)

        # 2. Extraction
        logger.debug(f"Starting DSPy Extraction for '{params.target_fact_name}'")
        t0 = time.perf_counter()

        # The `output_schema` provided acts as the exact keys the extraction should use.
        # If no output_schema is provided (output_schema=""), it falls back to using the target_fact_name (e.g., 'commissioner')
        # as the exact key of the extracted dictionary items. E.g., [{'commissioner': 'François-Xavier ORTOLI'}]
        prediction = self.extract(
            context=context,
            target_fact_name=params.target_fact_name,
            schema_description=_resolve_schema_description(params),
            question=params.question,
        )

        # Prompt logging
        _log_dspy_history("", params.target_fact_name, context=context)
        # 3. Verification (Optional)
        if params.verify_fact_name:
            logger.debug(
                f"Starting DSPy Verification against '{params.verify_fact_name}'"
            )
            t0 = time.perf_counter()

            v_facts = self.sql_store.select_fact4celex(
                params.celex_id, params.verify_fact_name
            )
            golden = [f.value_md for f in v_facts if f.value_md]

            audit = self.verify(
                context=context,
                extracted_list=str(prediction.output.extracted_facts),
                golden_reference_list=", ".join(golden),
                target_fact_name=params.target_fact_name,
            )
            _log_dspy_history("Verification", params.target_fact_name)
            logger.debug(
                f"DSPy Verification (incl. SQL lookup) took {time.perf_counter() - t0:.4f}s"
            )
            logger.debug(
                f"Verification Audit Hallucinations: {getattr(audit.output, 'hallucinations', 'N/A')}"
            )
            logger.debug(
                f"Verification Audit Verified Items: {audit.output.verified_facts}"
            )

            final_items = audit.output.verified_facts
            source_tag = f"{source}_verified"
        else:
            final_items = prediction.output.extracted_facts
            source_tag = source

        # 4. Extract Document References (Post-Verification)
        references_doc = None
        if source in ("hybrid", "full_doc") and final_items:
            logger.debug("Extracting supporting doc_pos references for verified items")
            ref_pred = self.find_doc_references(
                context=context, extracted_fact=str(final_items)
            )
            # strictly limit top-3
            refs = ref_pred.output.doc_positions
            if refs:
                references_doc = refs[:3]
            logger.debug(f"Found references: {references_doc}")

        return dspy.Prediction(
            output=FactOutput(
                items=final_items, source=source_tag, references_doc=references_doc
            )
        )


# -----------------------------------------


class DocKeywordSearchModule(dspy.Module):
    """
    A lightweight pipeline that bypasses LLMs, performing:
    1. SQL existence check returning True or False if the exact keyword was found in the entire celex document
    2. Hybrid keyword search fallback returning True or False if the exact keyword was found in the entire celex document

    Replies with [{target_fact_name: 'True'}] if hits are found.
    use case: leniency or legal Article 101 TFEU
    pipeline: keyword_search
    """

    def __init__(self, sql_store: SQLFactStore, searcher: KeywordSearch):
        super().__init__()
        self.sql_store = sql_store
        self.searcher = searcher

    def forward(self, params: FactExtractorParamVO):
        source = ""
        items = []
        references_doc = None

        prediction, _ = _check_fact_exists(self.sql_store, params, as_boolean=True)
        if prediction:
            return prediction

        source = "keyword"
        logger.debug(
            f"Starting Keyword lookup for target_fact '{params.target_fact_name}'"
        )
        t0 = time.perf_counter()
        # Direct lexical search leveraging searcher
        found = self.searcher.search(
            celex_id=params.celex_id,
            query=params.question,
            section_types=params.section_scope,
        )
        logger.debug(f"keyword search took {time.perf_counter() - t0:.4f}s")

        if found:
            items = [{params.target_fact_name: "True"}]
        else:
            items = [{params.target_fact_name: "False"}]

        return dspy.Prediction(
            output=FactOutput(items=items, source=source, references_doc=references_doc)
        )


class BookKeywordModule(dspy.Module):
    """Check whether a keyword exists in the book embedding table.

    A lightweight, LLM-free pipeline that:
      1. Optionally checks the SQL fact store for a cached result.
      2. Falls back to ``HybridBookSearchToolkit.keyword_exists`` for a
         direct ILIKE probe against the book embeddings.

    Replies with ``[{target_fact_name: 'True'}]`` when a hit is found,
    ``[{target_fact_name: 'False'}]`` otherwise.

    Args:
        sql_store: Fact persistence layer (SQL lookup / cache).
        book_searcher: Object satisfying the ``BookKeywordSearch`` protocol
            (e.g. ``HybridBookSearchToolkit``).

    pipeline: book_keyword_exists
    """

    def __init__(self, sql_store: SQLFactStore, book_searcher: BookKeywordSearch):
        super().__init__()
        self.sql_store = sql_store
        self.book_searcher = book_searcher

    def forward(self, params: FactExtractorParamVO):
        """Run keyword-existence check against book embeddings.

        Args:
            params: Extraction parameters including celex_id, target_fact_name,
                and question (used as the keyword to probe).

        Returns:
            ``dspy.Prediction`` wrapping a ``FactOutput`` with a single-item
            list indicating ``'True'`` or ``'False'``.
        """
        source = ""
        items: list[dict[str, str]] = []
        references_doc = None

        prediction, _ = _check_fact_exists(self.sql_store, params, as_boolean=True)
        if prediction:
            return prediction

        # lookup the case id's of the celex_id in
        #  SELECT distinct case_id FROM celex_facts.case_celex_id WHERE celex_id = '37784_8_4'
        case_ids = self.sql_store.select_case_ids_for_celex(params.celex_id)
        caseids_found_list = []
        logger.debug("Starting book keyword lookup for '%s'", params.target_fact_name)
        t0 = time.perf_counter()

        for caseid in case_ids:
            keyword_found = self.book_searcher.keyword_exists(keyword=caseid)
            if keyword_found:
                caseids_found_list.append({params.target_fact_name: caseid})
        logger.debug("Book keyword search took %.4fs", time.perf_counter() - t0)

        source = "book_keyword"
        items = (
            caseids_found_list
            if caseids_found_list
            else [{params.target_fact_name: "False"}]
        )

        return dspy.Prediction(
            output=FactOutput(
                items=items,
                source=source,
                references_doc=references_doc,
            )
        )


# -----------------------------------------


class BookLandmarkModule(dspy.Module):
    """Decide whether a CELEX's case is a landmark case in the book.

    A deterministic, LLM-free pipeline that:
      1. Optionally checks the SQL fact store for a cached result.
      2. For each case id of the celex, resolves the surface the book uses
         (trying punctuation variants) and reads the distinct section paths
         that name it.
      3. Classifies those paths with :func:`classify_book_reference`; the case
         is landmark when any case id is referenced under the book's
         "Economic landmark cases" subsection
         (``settings.BOOK_LANDMARK_SECTION_MARKERS``).

    Replies with ``[{target_fact_name: case_id}]`` for each landmark case id,
    or ``[{target_fact_name: 'False'}]`` when none qualify.

    Args:
        sql_store: Fact persistence layer (SQL lookup / cache, case-id lookup).
        book_chunk_repo: DAO exposing ``select_reference_paths`` over the book
            section embeddings.

    pipeline: book_landmark
    """

    def __init__(
        self, sql_store: SQLFactStore, book_chunk_repo: BookChunkRepository
    ):
        super().__init__()
        self.sql_store = sql_store
        self.book_chunk_repo = book_chunk_repo

    def forward(self, params: FactExtractorParamVO):
        """Classify the celex's case ids against the book's landmark subsection.

        Args:
            params: Extraction parameters including celex_id and
                target_fact_name.

        Returns:
            ``dspy.Prediction`` wrapping a ``FactOutput`` whose items list the
            landmark case ids, or a single ``'False'`` item when none qualify.
        """
        prediction, _ = _check_fact_exists(self.sql_store, params, as_boolean=True)
        if prediction:
            return prediction

        landmark_items: list[dict[str, str]] = []
        logger.debug(
            "Starting book landmark lookup for '%s'", params.target_fact_name
        )
        t0 = time.perf_counter()
        for case_id in self.sql_store.select_case_ids_for_celex(params.celex_id):
            for variant in case_id_variants(case_id):
                paths = self.book_chunk_repo.select_reference_paths(variant)
                if not paths:
                    continue
                # First present surface wins (mirrors _resolve_book_case_id).
                if classify_book_reference(paths) is BookReferenceType.LANDMARK:
                    landmark_items.append({params.target_fact_name: case_id})
                break
        logger.debug("Book landmark lookup took %.4fs", time.perf_counter() - t0)

        items = landmark_items or [{params.target_fact_name: "False"}]
        return dspy.Prediction(
            output=FactOutput(
                items=items,
                source="book_landmark",
                references_doc=None,
            )
        )


# -----------------------------------------


class BookFactModule(dspy.Module):
    """Hybrid book search + LLM extraction module.

    A pipeline that:
      1. Optionally checks the SQL fact store for a cached result.
      2. Falls back to ``BookSearcher.search`` to retrieve book
         chapter context via hybrid FTS + vector search.
      3. Calls the LLM via ``GenericExtractionSignature`` to extract
         structured facts from the retrieved context.

    Replies with a ``FactOutput`` containing extracted fact items.

    Args:
        sql_store: Fact persistence layer (SQL lookup / cache).
        book_searcher: Object satisfying the ``BookSearcher`` protocol
            (hybrid FTS + vector search over book embeddings).

    pipeline: book_fact_extract
    """

    def __init__(self, sql_store: SQLFactStore, book_searcher: BookSearcher):
        super().__init__()
        self.sql_store = sql_store
        self.book_searcher = book_searcher
        self.extract = dspy.ChainOfThought(GenericExtractionSignature)

    def forward(self, params: FactExtractorParamVO):
        """Run keyword-existence check against book embeddings.

        Args:
            params: Extraction parameters including celex_id, target_fact_name,
                and question (used as the keyword to probe).

        Returns:
            ``dspy.Prediction`` wrapping a ``FactOutput`` with a single-item
            list indicating ``'True'`` or ``'False'``.
        """
        context = ""
        source = ""

        references_doc = None

        prediction, _ = _check_fact_exists(self.sql_store, params, as_boolean=True)
        if prediction:
            return prediction
        source = "hybrid"
        logger.debug(
            f"Starting Hybrid lookup for target_fact '{params.target_fact_name}'"
        )
        t0 = time.perf_counter()
        ranked_chunks = self.book_searcher.search_semantic(query=params.question)
        logger.debug(f"Hybrid lookup took {time.perf_counter() - t0:.4f}s")
        context = "\n---\n".join(
            [f"[doc_pos: {c.doc_pos}]\n{c.document}" for c in ranked_chunks]
        )

        logger.debug(f"Context for '{params.target_fact_name}': {context[:40]}...")

        if not context:
            return dspy.Prediction(output=FactOutput(items=[], source="none"))

        _check_context_token_limit(context, params.target_fact_name)

        # 2. Extraction
        logger.debug(f"Starting DSPy Extraction for '{params.target_fact_name}'")
        t0 = time.perf_counter()

        # The `output_schema` provided acts as the exact keys the extraction should use.
        # If no output_schema is provided (output_schema=""), it falls back to using the target_fact_name (e.g., 'commissioner')
        # as the exact key of the extracted dictionary items. E.g., [{'commissioner': 'François-Xavier ORTOLI'}]

        prediction = self.extract(
            context=context,
            target_fact_name=params.target_fact_name,
            schema_description=_resolve_schema_description(params),
            question=params.question,
        )

        final_items = prediction.output.extracted_facts
        return dspy.Prediction(
            output=FactOutput(
                items=final_items,
                source=source,
                references_doc=references_doc,
            )
        )


# -----------------------------------------
class BookFactChapterTitleModule(dspy.Module):
    """Hybrid book search + LLM extraction module.

    A pipeline that:
      1. Optionally checks the SQL fact store for a cached result.
      2. Uses ``BookSearchToolkit.keyword_exists`` to filter to case IDs that
         appear in the book embeddings.
      3. Falls back to ``BookSearchToolkit.search`` to retrieve book
         chapter context via hybrid FTS + vector search.

    Replies with a ``FactOutput`` containing extracted fact items.

    Args:
        sql_store: Fact persistence layer (SQL lookup / cache).
        book_searcher: Object satisfying the ``BookSearchToolkit`` protocol
            (supports both ``keyword_exists`` and ``search``).

    pipeline: book_fact_extract
    """

    def __init__(self, sql_store: SQLFactStore, book_searcher: BookSearchToolkit):
        super().__init__()
        self.sql_store = sql_store
        self.book_searcher = book_searcher
        # self.extract = dspy.ChainOfThought(GenericExtractionSignature)

    def forward(self, params: FactExtractorParamVO):
        """Run keyword-existence check against book embeddings.

        Args:
            params: Extraction parameters including celex_id, target_fact_name,
                and question (used as the keyword to probe).

        Returns:
            ``dspy.Prediction`` wrapping a ``FactOutput`` with a single-item
            list indicating ``'True'`` or ``'False'``.
        """
        context = ""
        source = ""

        references_doc = None

        prediction, _ = _check_fact_exists(self.sql_store, params, as_boolean=True)
        if prediction:
            return prediction

        # lookup the case id's of the celex_id in
        #  SELECT distinct case_id FROM celex_facts.case_celex_id WHERE celex_id = '37784_8_4'
        case_ids = self.sql_store.select_case_ids_for_celex(params.celex_id)
        caseids_found_list = []
        logger.debug("Starting book keyword lookup for '%s'", params.target_fact_name)
        t0 = time.perf_counter()

        for caseid in case_ids:
            keyword_found = self.book_searcher.keyword_exists(keyword=caseid)
            if keyword_found:
                caseids_found_list.append({params.target_fact_name: caseid})
        logger.debug("Book keyword search took %.4fs", time.perf_counter() - t0)
        if not caseids_found_list:
            logger.debug("No case IDs found in book for celex_id '%s'", params.celex_id)
            return dspy.Prediction(output=FactOutput(items=[], source="none"))
        if len(caseids_found_list) > 1:
            logger.warning(
                "Multiple case_ids found for celex_id %s: %s. Using the first one for context retrieval.",
                params.celex_id,
                caseids_found_list,
            )
        source = "hybrid"
        logger.debug(
            f"Starting Hybrid lookup for target_fact '{params.target_fact_name}'"
        )
        t0 = time.perf_counter()
        ranked_chunks = self.book_searcher.search_keyword(
            keyword=caseids_found_list[0][params.target_fact_name]
        )
        logger.debug(f"Hybrid lookup took {time.perf_counter() - t0:.4f}s")

        # take the chapter titles only from the top retrieved chunks as context for extraction

        chapter_title = re.sub(
            r"^\d+\.\d+\s", "", ranked_chunks[0].chapter_title.rstrip("\n")
        )
        context = [{params.target_fact_name: chapter_title}]

        logger.debug(f"Context for '{params.target_fact_name}': {context[:40]}...")

        if not context:
            return dspy.Prediction(output=FactOutput(items=[], source="none"))

        return dspy.Prediction(
            output=FactOutput(
                items=context,
                source=source,
                references_doc=references_doc,
            )
        )


# -----------------------------------------
class DocHybridSearchModule(dspy.Module):
    """
    A lightweight pipeline that bypasses LLMs, performing:
    1. SQL existence check
    2. Hybrid keyword search fallback
    Replies with [{target_fact_name: 'True'}] if hits are found.

    pipeline: doc_search
    """

    def __init__(self, sql_store: SQLFactStore, searcher: HybridSearcher):
        super().__init__()
        self.sql_store = sql_store
        self.searcher = searcher

    def forward(self, params: FactExtractorParamVO):
        source = ""
        items = []
        references_doc = None

        prediction, _ = _check_fact_exists(self.sql_store, params, as_boolean=True)
        if prediction:
            return prediction
        #!! why is this hybrid?
        source = "hybrid"
        logger.debug(
            f"Starting Hybrid lookup for target_fact '{params.target_fact_name}'"
        )
        t0 = time.perf_counter()
        # Direct hybrid search leveraging searcher
        ranked_chunks = self.searcher.search(
            celex_id=params.celex_id,
            query=params.question,
            top_k=settings.DOC_SEARCH_TOP_K,
            section_types=params.section_scope,
        )
        logger.debug(f"Hybrid search took {time.perf_counter() - t0:.4f}s")

        if ranked_chunks:
            items = [{params.target_fact_name: "True"}]
            references_doc = [c.doc_pos for c in ranked_chunks][:3]

        return dspy.Prediction(
            output=FactOutput(items=items, source=source, references_doc=references_doc)
        )


class KeywordRegexModule(dspy.Module):
    """
    A lightweight pipeline that bypasses LLMs, performing:
    1. SQL existence check
    2. return all distinct terms matching the regex
    Replies with list of matching terms. Honors ``params.section_scope`` so a
    fact can read only the operative part of a decision (the articles it
    actually applies) rather than every narrative mention.

    pipeline: keyword_regex
    """

    def __init__(self, sql_store: SQLFactStore, searcher: RegexRetriever):
        super().__init__()
        self.sql_store = sql_store
        self.searcher = searcher

    def forward(self, params: FactExtractorParamVO):
        source = ""

        prediction, _ = _check_fact_exists(self.sql_store, params)
        if prediction:
            return prediction

        source = "regex"
        logger.debug(
            f"Starting Regex lookup for target_fact '{params.target_fact_name}'"
        )
        t0 = time.perf_counter()
        # Direct keyword search leveraging searcher
        terms = self.searcher.regex_retriever(
            celex_id=params.celex_id,
            regex=params.question,
            section_types=params.section_scope,
        )
        logger.debug(f"Regex search took {time.perf_counter() - t0:.4f}s")

        return dspy.Prediction(output=FactOutput(items=terms, source=source))


class InfringementConceptModule(dspy.Module):
    """Canonicalize a case's infringement concept(s) across treaty renumberings.

    A deterministic, LLM-free pipeline that reads the case's
    ``law_article_all_relevant`` (and, when available, its
    ``eu_commisssion_decision_date``) from the store and maps the literal article
    numbers to treaty-era-correct concepts via
    :func:`pipelines.legal.infringement_concept.concepts_for` -- the same
    normalizer backing the ``same_concept(...)`` rule-engine condition, so the
    persisted fact and the gating conditions can never diverge.

    The decision date sits deep in the heideltime chain and may not be computed
    yet on a fresh pass; the normalizer falls back to the year embedded in the
    CELEX id (which the lone ``Article 86`` token needs), so the concept resolves
    deterministically either way.

    pipeline: infringement_concept
    """

    _ARTICLE_FACT_NAME = "law_article_all_relevant"
    _DECISION_DATE_FACT_NAME = "eu_commisssion_decision_date"
    _SOURCE = "infringement_concept"

    def __init__(self, sql_store: SQLFactStore):
        super().__init__()
        self.sql_store = sql_store

    def forward(self, params: FactExtractorParamVO) -> dspy.Prediction:
        """Derive and emit the canonical infringement concept(s) for a case.

        Parameters
        ----------
        params:
            Extraction parameters; ``celex_id`` drives the input reads and the
            CELEX-year fallback, ``target_fact_name`` keys the emitted row.

        Returns
        -------
        dspy.Prediction
            A ``FactOutput`` whose ``items`` hold a single ``name -> concepts``
            row (sorted, comma-joined) or an empty list when no competition
            article is present.
        """
        prediction, _ = _check_fact_exists(self.sql_store, params)
        if prediction:
            return prediction

        article_text = self._latest_value(params.celex_id, self._ARTICLE_FACT_NAME)
        decision_date_value = self._latest_value(
            params.celex_id, self._DECISION_DATE_FACT_NAME
        )

        concepts = concepts_for(article_text, decision_date_value, params.celex_id)
        items = (
            [{params.target_fact_name: ", ".join(sorted(concepts))}]
            if concepts
            else []
        )
        return dspy.Prediction(
            output=FactOutput(items=items, source=self._SOURCE, references_doc=None)
        )

    def _latest_value(self, celex_id: str, fact_name: str) -> str:
        """Return the most recent ``value_md`` for a fact, or ``""`` when absent.

        Read best-effort: a missing or empty input simply yields no concept
        rather than raising, so a non-competition document never errors.
        """
        facts = self.sql_store.select_fact4celex(celex_id, fact_name)
        if not facts:
            return ""
        return facts[-1].value_md or ""


# -----------------------------------------


class FactContextModule(dspy.Module):
    """
    A pipeline variant that entirely bypasses semantic hybrid search, acting strictly on
    the extracted textual context of its defined `predecessors` dependencies.
    """

    def __init__(self, sql_store: SQLFactStore):
        super().__init__()
        self.sql_store = sql_store
        self.extract = dspy.ChainOfThought(DependentFactExtractionSignature)

    def forward(self, params: FactExtractorParamVO):
        source = "facts_context"

        prediction, _ = _check_fact_exists(self.sql_store, params, source_name=source)
        if prediction:
            return prediction

        # Gather context from predecessors
        context_parts = []
        for antecedent in params.predecessors:
            ant_facts = self.sql_store.select_fact4celex(params.celex_id, antecedent)
            if not ant_facts or not any(f.value_md for f in ant_facts):
                logger.error(
                    f"Antecedent '{antecedent}' missing or empty for '{params.target_fact_name}'."
                )
                raise ValueError(f"Missing prerequisite fact: '{antecedent}'")

            if any(f.value_md == "NA" for f in ant_facts):
                logger.info(
                    f"Antecedent '{antecedent}' is 'NA'. Short-circuiting extraction for '{params.target_fact_name}' and mapping to empty output."
                )
                return dspy.Prediction(
                    output=FactOutput(items=[], source=source, references_doc=None)
                )

            ant_md = "\n".join([f.value_md for f in ant_facts if f.value_md])
            context_parts.append(f"--- Context from {antecedent} ---\n{ant_md}")

        context = "\n\n".join(context_parts)

        _check_context_token_limit(context, params.target_fact_name)

        logger.debug(
            f"Starting Dependency DSPy Extraction for '{params.target_fact_name}'"
        )
        prediction = self.extract(
            context=context,
            target_fact_name=params.target_fact_name,
            schema_description=_resolve_schema_description(params),
            question=params.question,
        )
        _log_dspy_history("context-based", params.target_fact_name, use_info=True)
        logger.debug(f"Prediction: {prediction}")
        return dspy.Prediction(
            output=FactOutput(
                items=prediction.output.extracted_facts,
                source=source,
                references_doc=None,
            )
        )


class FactDetailRetriever(dspy.Module):
    """Per-entity detail enrichment over a predecessor roster.

    A pipeline variant combining ``universal`` (document retrieval) and
    ``fact_context`` (predecessor-driven): the FIRST entry of
    ``params.predecessors`` names the roster fact (e.g. ``companies_investigated``)
    whose rows define the entities to enrich. For each entity:

      1. Targeted retrieval: ``KeywordSearch`` with the exact entity name;
         when that returns nothing, fall back to ``HybridSearch`` with
         ``"<entity> <question>"``. Both calls honor ``params.section_scope``
         and are capped at ``settings.DETAIL_RETRIEVER_TOP_K`` chunks.
      2. One LLM extraction call (``EntityDetailExtractionSignature``) over the
         per-entity context.
      3. Defensive row assembly: keys are exactly the ``output_schema`` keys,
         the first key always carries the roster entity name verbatim, and
         missing detail values become the literal string ``'NA'``.

    Entities are never dropped: an entity without retrievable detail yields an
    all-``'NA'`` detail row. Entity names are read from the roster fact's
    ``value_json`` rows (first key of each row dict); a roster without
    structured ``value_json`` is a hard error.

    pipeline: detail_retriever
    """

    _SOURCE = "detail_retriever"

    def __init__(
        self,
        sql_store: SQLFactStore,
        keyword_search: KeywordSearch,
        hybrid_search: HybridSearcher,
    ):
        """Provision the module with its store and both search legs.

        Args:
            sql_store: Fact persistence layer (cache check and roster reads).
            keyword_search: Exact lexical search leg used first per entity.
            hybrid_search: FTS + dense hybrid leg used as fallback per entity.
        """
        super().__init__()
        self.sql_store = sql_store
        self.keyword_search = keyword_search
        self.hybrid_search = hybrid_search
        self.extract = dspy.ChainOfThought(EntityDetailExtractionSignature)

    def forward(self, params: FactExtractorParamVO) -> dspy.Prediction:
        """Enrich every roster entity with the detail fields of ``output_schema``.

        Args:
            params: Extraction parameters; ``predecessors[0]`` names the roster
                fact, ``output_schema`` defines the row keys (first key is the
                entity column), ``question`` steers the per-entity extraction.

        Returns:
            ``dspy.Prediction`` wrapping a ``FactOutput`` with one dict per
            roster entity and up to ``_MAX_REFERENCES_DOC`` supporting
            ``doc_pos`` references.

        Raises:
            ValueError: If no predecessor is configured, the roster fact is
                missing or lacks ``value_json``, or ``output_schema`` is empty.
        """
        prediction, _ = _check_fact_exists(
            self.sql_store, params, source_name=self._SOURCE
        )
        if prediction:
            return prediction

        roster_name = self._resolve_roster_name(params)
        entities = self._load_roster_entities(params.celex_id, roster_name)
        if not entities:
            logger.info(
                "Roster '%s' yields no entities for '%s'; emitting empty output.",
                roster_name,
                params.target_fact_name,
            )
            return dspy.Prediction(
                output=FactOutput(items=[], source=self._SOURCE, references_doc=None)
            )

        schema_keys = [
            key.strip() for key in params.output_schema.split(",") if key.strip()
        ]
        if len(schema_keys) < 2:
            raise ValueError(
                f"detail_retriever requires an output_schema with an entity key and "
                f"at least one detail key for '{params.target_fact_name}', "
                f"got '{params.output_schema}'."
            )

        rows: list[dict[str, Any]] = []
        references: list[int] = []
        for entity in entities:
            chunks = self._retrieve_chunks(params, entity)
            if not chunks:
                logger.warning(
                    "No chunks found for entity '%s' of '%s'; emitting NA row.",
                    entity,
                    params.target_fact_name,
                )
                rows.append(self._build_row(schema_keys, entity, {}))
                continue

            context = "\n---\n".join(
                [f"[doc_pos: {c.doc_pos}]\n{c.chunk}" for c in chunks]
            )
            _check_context_token_limit(
                context, f"{params.target_fact_name}:{entity}"
            )

            prediction = self.extract(
                context=context,
                entity=entity,
                target_fact_name=params.target_fact_name,
                schema_description=_resolve_schema_description(params),
                question=params.question,
            )
            _log_dspy_history(
                "detail", f"{params.target_fact_name}:{entity}", context=context
            )

            extracted = prediction.output.extracted_facts
            if len(extracted) != 1:
                logger.warning(
                    "Entity '%s' extraction returned %d dicts (expected 1); "
                    "using the first when present.",
                    entity,
                    len(extracted),
                )
            raw_row = extracted[0] if extracted else {}
            rows.append(self._build_row(schema_keys, entity, raw_row))

            top_doc_pos = chunks[0].doc_pos
            if top_doc_pos not in references:
                references.append(top_doc_pos)

        return dspy.Prediction(
            output=FactOutput(
                items=rows,
                source=self._SOURCE,
                references_doc=references[:_MAX_REFERENCES_DOC] or None,
            )
        )

    def _resolve_roster_name(self, params: FactExtractorParamVO) -> str:
        """Return the roster fact name (first predecessor), warning on extras.

        Raises:
            ValueError: If ``params.predecessors`` is empty.
        """
        if not params.predecessors:
            raise ValueError(
                f"detail_retriever requires a predecessor roster for "
                f"'{params.target_fact_name}'."
            )
        if len(params.predecessors) > 1:
            logger.warning(
                "detail_retriever uses only the first predecessor '%s' as the "
                "entity roster for '%s'; ignoring extras: %s",
                params.predecessors[0],
                params.target_fact_name,
                params.predecessors[1:],
            )
        return params.predecessors[0]

    def _load_roster_entities(self, celex_id: str, roster_name: str) -> list[str]:
        """Return deduplicated entity names from the roster fact's ``value_json``.

        The last roster row wins (``facts[-1]`` convention). ``value_json`` may
        arrive either as a JSON string (live DB ``String`` column) or as an
        already-deserialized list (tests, cached paths); both are accepted.
        Per row the FIRST key's value is the entity name; empty and ``'NA'``
        values are skipped.

        Returns:
            Ordered, deduplicated entity names. Empty when the roster fact is
            the ``'NA'`` placeholder.

        Raises:
            ValueError: If the roster fact is missing/empty or carries no
                structured ``value_json``.
        """
        roster_facts = self.sql_store.select_fact4celex(celex_id, roster_name)
        if not roster_facts:
            raise ValueError(f"Missing prerequisite fact: '{roster_name}'")

        if any(f.value_md == "NA" for f in roster_facts):
            logger.info(
                "Roster '%s' is 'NA' for %s; short-circuiting detail retrieval.",
                roster_name,
                celex_id,
            )
            return []

        value_json = roster_facts[-1].value_json
        if isinstance(value_json, str) and value_json.strip():
            try:
                value_json = json.loads(value_json)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Predecessor '{roster_name}' has malformed value_json for "
                    f"{celex_id}: {exc}"
                ) from exc
        if not isinstance(value_json, list) or not value_json:
            raise ValueError(
                f"Predecessor '{roster_name}' has no structured value_json for "
                f"{celex_id}; detail_retriever requires a structured roster."
            )

        entities: list[str] = []
        for row in value_json:
            if not isinstance(row, dict) or not row:
                logger.warning(
                    "Skipping malformed roster row %r of '%s'.", row, roster_name
                )
                continue
            entity = str(next(iter(row.values())) or "").strip()
            if not entity or entity.upper() == "NA":
                logger.warning(
                    "Skipping empty/NA roster entity in '%s' for %s.",
                    roster_name,
                    celex_id,
                )
                continue
            if entity not in entities:
                entities.append(entity)
        return entities

    def _retrieve_chunks(self, params: FactExtractorParamVO, entity: str) -> list:
        """Retrieve per-entity chunks: keyword leg first, hybrid fallback.

        Args:
            params: Extraction parameters (celex_id, question, section_scope).
            entity: The entity name driving the targeted search.

        Returns:
            Ranked chunks for the entity; empty when both legs miss.
        """
        t0 = time.perf_counter()
        chunks = self.keyword_search.search(
            celex_id=params.celex_id,
            query=entity,
            top_k=settings.DETAIL_RETRIEVER_TOP_K,
            section_types=params.section_scope,
        )
        if not chunks:
            logger.debug(
                "Keyword leg empty for entity '%s'; falling back to hybrid.",
                entity,
            )
            chunks = self.hybrid_search.search(
                celex_id=params.celex_id,
                query=f"{entity} {params.question}",
                top_k=settings.DETAIL_RETRIEVER_TOP_K,
                section_types=params.section_scope,
            )
        logger.debug(
            "Entity '%s' retrieval took %.4fs (%d chunks).",
            entity,
            time.perf_counter() - t0,
            len(chunks),
        )
        return chunks

    @staticmethod
    def _build_row(
        schema_keys: list[str], entity: str, raw_row: dict[str, Any]
    ) -> dict[str, str]:
        """Assemble one output row with exactly the schema keys.

        The entity column always carries the roster name (never the LLM's),
        detail values are taken by exact key from ``raw_row``, and blank or
        placeholder values are normalized to the literal ``'NA'``.
        """
        row = {schema_keys[0]: entity}
        for key in schema_keys[1:]:
            value = str(raw_row.get(key, "") or "").strip()
            row[key] = value if value and value.lower() not in _NA_SENTINELS else "NA"
        return row


class FactComparisonModule(dspy.Module):
    """
    A specialized pipeline to compare extracted facts from the legacy rule_facts.facts
    table against celex_facts.facts, evaluating if they convey the same core truth.
    Output is standard FactOutput with dynamic flag [fact_name]_match.
    """

    def __init__(self, sql_store: SQLFactStore):
        super().__init__()
        self.sql_store = sql_store
        self.compare = dspy.ChainOfThought(FactComparisonSignature)

    def forward(self, params: FactExtractorParamVO):
        source = "comparison"
        target_name = params.target_fact_name

        # 1. Fetch from rule_facts
        rule_facts = self.sql_store.select_legacy_fact4celex(
            params.celex_id, target_name
        )
        if not rule_facts:
            logger.info(
                f"No legacy rule_fact found for '{target_name}'. returning non-match."
            )
            items = [
                {
                    f"{target_name}_match": "False",
                    "reasoning": "Missing legacy rule_fact",
                }
            ]
            return dspy.Prediction(output=FactOutput(items=items, source=source))

        rule_val = str([f.value for f in rule_facts])

        # 2. Fetch from celex_facts
        llm_facts = self.sql_store.select_fact4celex(params.celex_id, target_name)
        if not llm_facts:
            logger.info(
                f"No LLM celex_fact found for '{target_name}'. returning non-match."
            )
            items = [
                {f"{target_name}_match": "False", "reasoning": "Missing celex_fact"}
            ]
            return dspy.Prediction(output=FactOutput(items=items, source=source))

        # extract JSON representations if available, fallback to markdown
        llm_val = str([f.value_json if f.value_json else f.value_md for f in llm_facts])

        # 3. DSPy Comparison
        logger.debug(
            f"Comparing '{target_name}' values. Rule: {rule_val[:50]} vs LLM: {llm_val[:50]}"
        )
        prediction = self.compare(
            question=params.question if params.question else target_name,
            rule_fact=rule_val,
            llm_fact=llm_val,
        )

        _log_dspy_history("Comparison", target_name)

        items = [
            {
                f"{target_name}_match": str(prediction.output.is_match),
                "reasoning": prediction.output.reasoning,
            }
        ]

        return dspy.Prediction(output=FactOutput(items=items, source=source))


# -----------------------------------------------------------------------------


class NaceModule(dspy.Module):
    """
    A specialized pipeline to match a NACE class code for a product and a NACE group code.
    DSPy agent is used but not the LLM invoker dspy.Prediction
    Output is standard FactOutput with dynamic flag [fact_name]_match.
    """

    def __init__(
        self, sql_store: SQLFactStore, nace_tree: NaceTree, ranker: RankerProtocol
    ):
        super().__init__()

        self.sql_store = sql_store
        self.nace_tree = nace_tree
        self.ranker = ranker

    def forward(self, params: FactExtractorParamVO):

        nace_group_code = None
        product = None
        fact_list = params.predecessors
        for fact_name in fact_list:
            if "product" in fact_name.lower():
                product_facts = self.sql_store.select_fact4celex(
                    params.celex_id, fact_name
                )
                product = product_facts[-1].value_md if product_facts else None
            elif "nace_group_code" in fact_name.lower():
                nace_group_facts = self.sql_store.select_fact4celex(
                    params.celex_id, fact_name
                )
                nace_group_code = (
                    nace_group_facts[-1].value_md if nace_group_facts else None
                )

        # build NACE VO nace_group_code
        if not product or not nace_group_code:
            logger.info(
                "No product or NACE group code available; skipping NACE lookup."
            )
            return dspy.Prediction(output=FactOutput(items=[], source="nace"))

        parts = nace_group_code.split(":", 1)
        nace_title = parts[1].strip() if len(parts) > 1 else nace_group_code.strip()
        naceVo = self.nace_tree.select_group_by_title(nace_title)
        classes = self.nace_tree.list_nacetree_classes_of_group(naceVo)
        if not classes:
            logger.info(f"No NACE classes found for group code '{nace_group_code}'.")
            return dspy.Prediction(output=FactOutput(items=[], source="nace"))

        ranked = self.ranker.rank_nacevo(product, classes, top_k=1)
        top = ranked[0]
        logger.debug(
            f"Top ranked NACE class for product '{product}' and group '{nace_group_code}': {top.code} {top.title}"
        )
        # rank with flashranker
        dict_nace = {}
        dict_nace["nace_class_code"] = str(top.code) + ": " + str(top.title)
        return dspy.Prediction(output=FactOutput(items=[dict_nace], source="nace"))


class FactSummaryModule(dspy.Module):
    """Summarize the catalog's key facts of a celex_id into structured chapters.

    The summarized facts are exactly ``params.context_facts`` (the
    ``context_facts:`` list of the ``case_summary`` YAML entry), read
    best-effort: a fact with no stored row is skipped, never fatal. A stored
    summary is served from the fact table unless ``force_override`` is set or
    one of its input facts was written after it, in which case it is rebuilt.
    """

    def __init__(self, sql_store: SQLFactStore):
        """Provision the fact store and the summarizing predictor.

        Args:
            sql_store: Fact store providing ``select_fact4celex`` and
                ``select_named_facts4celex``.
        """
        super().__init__()
        self.sql_store = sql_store
        self.summarize = dspy.ChainOfThought(CaseSummarySignature)

    def forward(self, params: FactExtractorParamVO) -> dspy.Prediction:
        """Return the stored summary when current, otherwise generate a new one.

        Args:
            params: Fact config; ``context_facts`` names the facts to summarize.

        Returns:
            Prediction whose ``output`` holds one ``{"case_summary": markdown}``
            item, or no items (source ``none``) when no input fact is stored.
        """
        source = "summary"

        if not params.context_facts:
            logger.error(
                "Fact '%s' declares no context_facts; nothing to summarize.",
                params.target_fact_name,
            )
            return dspy.Prediction(output=FactOutput(items=[], source="none"))

        input_facts = [
            fact
            for fact in self.sql_store.select_named_facts4celex(
                params.celex_id, params.context_facts
            )
            if (fact.value_md or "").strip()
        ]

        prediction, stored = _check_fact_exists(self.sql_store, params)
        if prediction and not self._is_stale(stored[-1], input_facts):
            return prediction

        if not input_facts:
            logger.info(
                f"No facts found for celex_id {params.celex_id}. Cannot generate summary."
            )
            return dspy.Prediction(output=FactOutput(items=[], source="none"))

        missing = set(params.context_facts) - {fact.name for fact in input_facts}
        if missing:
            logger.debug(
                "Summary for %s built without absent facts: %s",
                params.celex_id,
                sorted(missing),
            )
        context = "\n\n".join(f"{fact.name}:\n{fact.value_md}" for fact in input_facts)

        _check_context_token_limit(context, params.celex_id)

        logger.debug(
            f"Context for Summary DSPy Generation for '{params.celex_id}':\n{context}"
        )
        logger.debug(f"Starting Summary DSPy Generation for '{params.celex_id}'")
        t0 = time.perf_counter()
        prediction_output = self.summarize(context=context)

        _log_dspy_history("Summary", params.celex_id)

        logger.debug(f"Summary Generation took {time.perf_counter() - t0:.4f}s")

        # Format the Pydantic model into a nice Markdown wiki-style string with headings
        summary_dict = prediction_output.case_summary.model_dump()
        md_lines = []
        for key, value in summary_dict.items():
            # Create a nice human-readable title from the field name (e.g., 'key_timeline' -> 'Key Timeline')
            title = " ".join(word.capitalize() for word in key.split("_"))
            md_lines.append(f"### {title}\n{value}\n")

        formatted_summary = "\n".join(md_lines)

        # add a suffix to the  formatted_summary with current datetime in format YYYY-MM-DD HH:MM:SS and in brackets and font italics
        current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted_summary += f"\n\n*(reported: {current_datetime})*"

        items = [{"case_summary": formatted_summary}]

        return dspy.Prediction(
            output=FactOutput(items=items, source=source, references_doc=None)
        )

    @staticmethod
    def _is_stale(summary: Any, input_facts: list) -> bool:
        """Return True when an input fact was written after the stored summary.

        Args:
            summary: The stored summary ``Facts`` row.
            input_facts: The ``Facts`` rows the summary is built from.

        Returns:
            True if any input fact's ``created_at`` is newer than the summary's.
        """
        newer = [
            fact.name
            for fact in input_facts
            if fact.created_at and summary.created_at
            and fact.created_at > summary.created_at
        ]
        if newer:
            logger.info(
                "Stored summary for %s predates facts %s; regenerating.",
                summary.celex_id,
                newer,
            )
        return bool(newer)


# -----------------------------------------


class CelexCaseIdModule(dspy.Module):
    """Pure SQL lookup that maps a celex_id to its associated case_id values.

    Queries celex_facts.case_celex_id via the SQLFactStore protocol and
    returns one dict per case_id found.

    pipeline: celex_case_id
    """

    def __init__(self, sql_store: SQLFactStore):
        """Provision the module with a SQL fact store.

        Args:
            sql_store: Data-access object satisfying the SQLFactStore protocol.
        """
        super().__init__()
        self.sql_store = sql_store

    def forward(self, params: FactExtractorParamVO) -> dspy.Prediction:
        """Return celex_id/case_id pairs from the mapping table.

        Args:
            params: Extraction parameters; only celex_id and force_override are used.

        Returns:
            dspy.Prediction whose output is a FactOutput with items as a list of
            {"celex_id": ..., "case_id": ...} dicts, one per associated case.
        """
        prediction, _ = _check_fact_exists(self.sql_store, params)
        if prediction:
            return prediction

        logger.debug(f"Starting celex_case_id lookup for '{params.celex_id}'")
        t0 = time.perf_counter()
        case_ids = self.sql_store.select_case_ids_for_celex(params.celex_id)
        logger.debug(f"celex_case_id lookup took {time.perf_counter() - t0:.4f}s")

        items = [{"celex_id": params.celex_id, "case_id": cid} for cid in case_ids]

        return dspy.Prediction(
            output=FactOutput(items=items, source="sql_lookup", references_doc=None)
        )


class FactCaseTitleModule(dspy.Module):
    """
    Extracts the exact case ID and title from the first page of the document.

    Queries the first chunk (doc_pos = 0) of a celex_id from celex_facts.pg_embedding_qwen
    and uses a DSPy Signature to extract the entire case title row.

    pipeline: case_title
    """

    def __init__(self, sql_store: SQLFactStore):
        """Provision the module with a SQL fact store."""
        super().__init__()
        self.sql_store = sql_store
        self.extract = dspy.Predict(FactCaseTitleSignature)

    def forward(self, params: FactExtractorParamVO):
        source = ""
        items = []

        prediction, _ = _check_fact_exists(self.sql_store, params)
        if prediction:
            return prediction

        source = "sql_doc_pos_0"
        logger.debug(f"Starting FactCaseTitle lookup for '{params.celex_id}'")
        t0 = time.perf_counter()

        # 1. SQL Retrieval Orchestration
        first_page = self.sql_store.select_first_page4celex(params.celex_id)
        logger.debug(f"SQL first page lookup took {time.perf_counter() - t0:.4f}s")

        if not first_page:
            logger.debug(f"No first page found for celex_id '{params.celex_id}'.")
            return dspy.Prediction(
                output=FactOutput(items=[], source="none", references_doc=None)
            )

        _check_context_token_limit(first_page.document, params.celex_id)

        # 2. DSPy Extraction
        logger.debug(f"Starting DSPy Extraction for '{params.target_fact_name}'")
        t0 = time.perf_counter()

        extraction = self.extract(context=first_page.document)

        _log_dspy_history("Case Title", params.celex_id)

        logger.debug(f"Extraction took {time.perf_counter() - t0:.4f}s")

        case_id = extraction.output.case_id
        title = extraction.output.title
        if case_id or title:
            items = [{params.target_fact_name: (case_id, title)}]

        # 3. Emit explicit chunk lineage from the real first-page chunk identity
        # rather than a hardcoded positional reference. The ingestor persists
        # these LineageSourceVO instances directly (see FactOutput.lineage_sources).
        references_doc = None
        lineage_sources = None
        if items:
            references_doc = [first_page.doc_pos]
            lineage_sources = [
                LineageSourceVO(
                    source_type="chunk",
                    celex_id=params.celex_id,
                    chunk_id=first_page.chunk_id,
                    chunk_seq=[first_page.doc_pos],
                    extraction_method=params.target_fact_name,
                    pipeline=params.pipeline,
                    dspy_module=self.__class__.__name__,
                )
            ]

        return dspy.Prediction(
            output=FactOutput(
                items=items,
                source=source,
                references_doc=references_doc,
                lineage_sources=lineage_sources,
            )
        )
