"""
DSPy module wrapping HeidelTime temporal-expression extraction.

The module is deterministic (JAR-based) — no LLM call is made.  It replicates
the chunk-selection and persistence logic of
:func:`~batch.Main_ingest_heideltime.ingest_heideltime_for_celex`, wrapped in
the :class:`~dspy.Module` interface so it can be dispatched by
:class:`~batch.Main_ingest_generic_facts.PipelineExecutor`.

pipeline name: heideltime_raw
"""

import hashlib
import logging
import re
from uuid import UUID

import dspy
from sqlalchemy import text

from pipelines.rag.pgvector.models import HeidelTimeVO
from config.settings import settings
from pipelines.agent_dspy.dspy_protocols import (
    FactOutput,
    HeidelTimeEventRetriever,
    SQLFactStore,
)
from pipelines.agent_dspy.dspy_signature import (
    HeidelTimeCriticalEvent,
    HeidelTimeEventAggregateSignature,
    HeidelTimeEventSignature,
    HeideltimeCartelPeriodSignature,
    HeideltimeConductEventsSignature,
)
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import (
    _check_context_token_limit,
    _get_tiktoken_enc,
)
from pipelines.heideltime.event_dates import parse_partial_date
from pipelines.heideltime.event_digest import (
    EventLine,
    digest_events,
    render_digest,
    split_batches,
)
from pipelines.heideltime.extract_heideltime import HeidelTime
from pipelines.heideltime.period_inheritance import (
    INHERITANCE_METHOD,
    InheritanceBasis,
    InheritedPeriod,
    PeriodInheritor,
)
from pipelines.rag.pgvector.heideltime_ingestor import HeidelTimeIngestor
from tools.PipelineFactIngestor import LineageSourceVO

logger = logging.getLogger(__name__)


def _chunk_hash(document: str) -> str:
    """Return the md5 hex digest of *document*, matching PostgreSQL ``md5()``.

    The digest is stored in ``heideltime_extractions.source_hash`` and compared
    server-side against ``md5(pg_embedding_qwen.document)``, so it must be the
    digest of the same bytes.  PostgreSQL hashes the text in the server
    encoding (UTF-8 for this database), hence the explicit ``utf-8`` encode
    rather than the platform default.

    Args:
        document: Chunk text exactly as it was handed to the HeidelTime JAR.

    Returns:
        32-character lowercase hex digest.
    """
    return hashlib.md5(document.encode("utf-8")).hexdigest()


class HeidelTimeModule(dspy.Module):
    """DSPy pipeline for HeidelTime temporal-expression extraction.

    Not LLM-based: wraps the HeidelTime JAR extractor directly and persists
    results via :class:`~pipelines.rag.pgvector.heideltime_ingestor.HeidelTimeIngestor`.

    Implements the Provisioner pattern: ``ht`` and ``ingestor`` are injected in
    ``__init__`` and reused across all ``forward`` calls.

    The fact asserts what the JAR found, so its evidence is exactly the rows it
    wrote: every ``heideltime_extractions`` row becomes a ``heideltime`` source
    and every chunk they came from a ``chunk`` source, returned as
    ``FactOutput.lineage_sources``.  Without them the fact is a terminal node
    carrying no evidence, and ``fact_provenance()`` — which emits only
    non-``fact`` sources — resolves the whole derived chain above it to nothing.

    pipeline: heideltime_raw

    Args:
        sql_store: SQL fact store used for the fast-return existence check.
        ht: HeidelTime JAR wrapper (shared across chunks).
        ingestor: Persistence provisioner for reading chunks and writing extractions.
    """

    def __init__(
        self,
        sql_store: SQLFactStore,
        ht: HeidelTime,
        ingestor: HeidelTimeIngestor,
    ) -> None:
        super().__init__()
        self.sql_store = sql_store
        self.ht = ht
        self.ingestor = ingestor

    def forward(self, params: FactExtractorParamVO) -> dspy.Prediction:
        """Extract temporal expressions for all unprocessed chunks of a CELEX document.

        Bulk-fetches the chunks, keeps the ones whose stored extractions
        still match ``md5()`` of their current text, deletes and re-runs the
        rest, and returns a :class:`FactOutput`.  ``force_override`` marks every
        chunk stale.

        Staleness is what makes this correct across an in-place chunk-text
        repair: a chunk that produced rows against the *old* text is no longer
        "already processed", because idempotence is not freshness.

        Args:
            params: Extraction parameters including ``celex_id`` and ``force_override``.

        Returns:
            A :class:`~dspy.Prediction` whose ``output`` is a lightweight proxy
            payload describing the raw data stored in
            ``celex_facts.heideltime_extractions``, carrying one lineage source
            per extraction row and per chunk those rows came from.
        """
        # 1. Bulk-fetch all source chunks for the CELEX document.  This now
        # precedes the fast-return: the proxy payload alone cannot tell whether
        # the stored rows still describe the text that is in the table, so the
        # staleness probe has to run before we are allowed to short-circuit.
        with self.ingestor.Session() as session:
            stmt = text(
                "SELECT id, document"
                f" FROM celex_facts.{settings.EMBEDDING_TABLE}"
                " WHERE celex_id = :celex_id"
                " ORDER BY doc_pos ASC"
            )
            chunks = session.execute(stmt, {"celex_id": params.celex_id}).fetchall()

        if not chunks:
            logger.info("No chunks found for CELEX %s.", params.celex_id)
            return dspy.Prediction(output=FactOutput(items=[], source="none"))

        # 2. Decide which chunks are stale.  A chunk is fresh only while every
        # stored row carries a source_hash equal to md5() of its current text,
        # so an in-place text repair (scan_dates.py) invalidates it without any
        # operator having to remember to pass --overwrite.  --overwrite still
        # forces the whole document, which is what the old code's TODO on the
        # chunk filter was missing: force_override used to gate only the
        # fast-return, never the per-chunk skip.
        chunk_ids = {str(chunk.id) for chunk in chunks}
        if params.force_override:
            stale_ids = chunk_ids
        else:
            stale_ids = chunk_ids - self.ingestor.fresh_chunk_ids(params.celex_id)

        logger.info(
            "CELEX %s: %d total chunks, %d fresh, %d to (re)process%s.",
            params.celex_id,
            len(chunks),
            len(chunk_ids) - len(stale_ids),
            len(stale_ids),
            " (forced)" if params.force_override else "",
        )

        # 3. Fast-return from the raw HeidelTime table when every chunk is
        # fresh.  Lineage is rebuilt even on this path, so re-running the
        # pipeline heals missing provenance without re-invoking the JAR.
        if not stale_ids and not params.force_override:
            proxy_items = self.ingestor.build_proxy_fact_payload(params.celex_id)
            if proxy_items:
                return dspy.Prediction(
                    output=FactOutput(
                        items=proxy_items,
                        source="heideltime_extractions",
                        lineage_sources=self._build_lineage_sources(params) or None,
                    )
                )

        # 4. Drop the stale rows before re-extracting.  ingest_vos() inserts
        # ON CONFLICT DO NOTHING against (celex_id, fk_chunk_id, tid), so a
        # surviving old row would silently win over its correction; the delete
        # also clears the lineage registry entries citing those rows, which
        # carry no foreign key and would otherwise outlive them as orphans.
        if stale_ids:
            deleted = self.ingestor.delete_chunks(stale_ids)
            if deleted:
                logger.info(
                    "CELEX %s: removed %d stale extraction rows before re-run.",
                    params.celex_id,
                    deleted,
                )

        # 5. Run HeidelTime per stale chunk and persist via ingestor, stamping
        # each row with the hash of the exact text it was extracted from.
        for chunk in chunks:
            if str(chunk.id) not in stale_ids:
                continue

            document = str(chunk.document) if chunk.document else ""
            if not document.strip():
                logger.debug("Empty document for chunk %s — skipping.", chunk.id)
                continue

            nested: list[list[HeidelTimeVO]] = self.ht.predict(
                celex_id=params.celex_id,
                fk_chunk_id=str(chunk.id),
                texts=[document],
            )
            vos: list[HeidelTimeVO] = nested[0] if nested else []
            if vos:
                self.ingestor.ingest_vos(vos, source_hash=_chunk_hash(document))
        # 6. Return a compact proxy payload derived from persisted raw rows,
        # with one lineage source per extraction row and per chunk behind it.
        items = self.ingestor.build_proxy_fact_payload(params.celex_id)
        return dspy.Prediction(
            output=FactOutput(
                items=items,
                source="heideltime_extractions" if items else "none",
                lineage_sources=(
                    self._build_lineage_sources(params) if items else None
                )
                or None,
            )
        )

    def _build_lineage_sources(
        self, params: FactExtractorParamVO
    ) -> list[LineageSourceVO]:
        """Build the evidence behind the ``heideltime_raw`` fact.

        The fact's value counts the extraction rows the JAR produced, so each of
        those rows is cited as a ``heideltime`` source, alongside a ``chunk``
        source for every distinct chunk they were found in.  The registry is
        normalised on natural keys, so chunks already catalogued by another fact
        are reused rather than duplicated.

        Args:
            params: Extraction parameters supplying ``celex_id``,
                ``target_fact_name`` and ``pipeline``.

        Returns:
            One :class:`~tools.PipelineFactIngestor.LineageSourceVO` per
            extraction row, followed by one per distinct chunk.  Empty when the
            document has no extractions.
        """
        extractions = self.ingestor.select_extraction_sources(params.celex_id)
        if not extractions:
            return []

        sources: list[LineageSourceVO] = [
            LineageSourceVO(
                source_type="heideltime",
                celex_id=params.celex_id,
                heideltime_event_id=UUID(row["id"]),
                description=row["timex_text"] or None,
                extraction_method=params.target_fact_name,
                pipeline=params.pipeline,
                dspy_module=self.__class__.__name__,
            )
            for row in extractions
        ]

        # One chunk source per distinct chunk, carrying its doc_pos for display.
        # A chunk dropped from the embedding table has no doc_pos; it is still
        # cited, just without the positional hint.
        chunk_positions: dict[str, int | None] = {}
        for row in extractions:
            chunk_positions.setdefault(row["fk_chunk_id"], row["doc_pos"])

        sources.extend(
            LineageSourceVO(
                source_type="chunk",
                celex_id=params.celex_id,
                chunk_id=chunk_id,
                chunk_seq=[doc_pos] if doc_pos is not None else None,
                extraction_method=params.target_fact_name,
                pipeline=params.pipeline,
                dspy_module=self.__class__.__name__,
            )
            for chunk_id, doc_pos in chunk_positions.items()
        )
        return sources


class HeidelTimeEventModule(dspy.Module):
    """DSPy pipeline for LLM-enriched event extraction from HeidelTime canonical dates.

    Reads pre-extracted dates from ``celex_facts.heideltime_extractions`` (populated
    by :class:`HeidelTimeModule`), sends each chunk's text and canonical dates to the
    LLM via :class:`~pipelines.agent_dspy.dspy_signature.HeidelTimeEventSignature`,
    and returns a :class:`~pipelines.agent_dspy.dspy_protocols.FactOutput` whose
    ``items`` are a list of ``{event_date, actor, predicate_object}`` dicts.

    ``PipelineFactIngestor.save_fact()`` converts ``items`` to:

    * ``value_json`` — JSON array of event dicts.
    * ``value_md`` — Markdown table via ``format_fact_to_markdown``.

    Every emitted event keeps the identity of the ``heideltime_extractions``
    row it came from, returned as ``FactOutput.lineage_sources`` so the ingestor
    records ``heideltime`` provenance on the normal write path.  Recording the
    link here — while the chunk that was shown to the LLM is still known — is
    exact, whereas re-deriving it afterwards can only match a date string
    against every extraction in the document.

    Implements the Provisioner pattern: ``sql_store`` and ``retriever`` are injected
    in ``__init__`` and shared across all ``forward`` calls.

    pipeline: heideltime_event

    Args:
        sql_store: SQL fact store for the fast-return existence check.
        retriever: Data-access object implementing
            :class:`~pipelines.agent_dspy.dspy_protocols.HeidelTimeEventRetriever`.
    """

    def __init__(
        self,
        sql_store: SQLFactStore,
        retriever: HeidelTimeEventRetriever,
    ) -> None:
        super().__init__()
        self.sql_store = sql_store
        self.retriever = retriever
        self.extract = dspy.ChainOfThought(HeidelTimeEventSignature)

    def forward(self, params: FactExtractorParamVO) -> dspy.Prediction:
        """Enrich HeidelTime dates with LLM-extracted events for a CELEX document.

        Args:
            params: Extraction parameters including ``celex_id``, ``target_fact_name``,
                and ``force_override``.

        Returns:
            A :class:`~dspy.Prediction` whose ``output`` is a
            :class:`~pipelines.agent_dspy.dspy_protocols.FactOutput` with one dict
            per event (keys: ``event_date``, ``actor``, ``predicate_object``) and
            one ``heideltime`` lineage source per contributing extraction row.
        """
        # 1. Fast-return if fact already persisted and override is not requested.
        if not params.force_override:
            facts = self.sql_store.select_fact4celex(
                params.celex_id, params.target_fact_name
            )
            if facts and facts[-1].value_json:
                return dspy.Prediction(
                    output=FactOutput(items=facts[-1].value_json, source="sql")
                )

        # 2. Retrieve chunks that have canonical HeidelTime dates.
        chunks = self.retriever.select_chunks_with_dates(params.celex_id)
        if not chunks:
            logger.info(
                "No heideltime extractions found for CELEX %s.", params.celex_id
            )
            return dspy.Prediction(output=FactOutput(items=[], source="none"))

        # 3. Per chunk: call LLM to extract structured events.  Only the dates
        # the LLM actually returned are credited to that chunk's extraction
        # rows, so an unused date contributes no provenance.
        all_events: list[dict[str, str]] = []
        extraction_texts: dict[str, str] = {}
        event_date_extractions: dict[str, set[str]] = {}
        for chunk in chunks:
            if not chunk["chunk_text"].strip() or not chunk["canonical_dates"]:
                continue
            canonical_dates_str = "\n".join(chunk["canonical_dates"])
            pred = self.extract(
                chunk_text=chunk["chunk_text"],
                canonical_dates=canonical_dates_str,
            )
            date_sources = chunk.get("date_sources") or {}
            for ev in pred.output.events:
                all_events.append(
                    {
                        "event_date": ev.event_date,
                        "actor": ev.actor,
                        "predicate_object": ev.predicate_object,
                    }
                )
                for source in date_sources.get(ev.event_date, []):
                    extraction_id = str(source["id"])
                    extraction_texts[extraction_id] = source.get("timex_text") or ""
                    event_date_extractions.setdefault(ev.event_date, set()).add(
                        extraction_id
                    )

        # 4. Deduplicate on (event_date, predicate_object) and sort chronologically.
        seen: set[tuple[str, str]] = set()
        unique_events: list[dict[str, str]] = []
        for ev in all_events:
            key = (ev["event_date"], ev["predicate_object"])
            if key not in seen:
                seen.add(key)
                unique_events.append(ev)
        unique_events.sort(key=lambda e: e.get("event_date", ""))

        # 5. Emit one lineage source per extraction row behind a surviving event.
        lineage_sources = self._build_lineage_sources(
            params=params,
            events=unique_events,
            event_date_extractions=event_date_extractions,
            extraction_texts=extraction_texts,
        )

        return dspy.Prediction(
            output=FactOutput(
                items=unique_events,
                source="heideltime_event",
                lineage_sources=lineage_sources or None,
            )
        )

    def _build_lineage_sources(
        self,
        params: FactExtractorParamVO,
        events: list[dict[str, str]],
        event_date_extractions: dict[str, set[str]],
        extraction_texts: dict[str, str],
    ) -> list[LineageSourceVO]:
        """Build ``heideltime`` lineage sources for the surviving events.

        Args:
            params: Extraction parameters supplying ``celex_id``,
                ``target_fact_name`` and ``pipeline``.
            events: Deduplicated events that will be persisted as the fact value.
            event_date_extractions: Event date -> ids of the
                ``heideltime_extractions`` rows that offered the date to the LLM.
            extraction_texts: Extraction id -> surface ``timex_text``, used as the
                registry row's human-readable description.

        Returns:
            One :class:`~tools.PipelineFactIngestor.LineageSourceVO` per distinct
            extraction row, ordered by event date then extraction id for a stable
            write order.  Empty when no event resolved to an extraction row.
        """
        sources: list[LineageSourceVO] = []
        registered: set[str] = set()
        for event in events:
            event_date = event.get("event_date", "")
            for extraction_id in sorted(event_date_extractions.get(event_date, ())):
                if extraction_id in registered:
                    continue
                registered.add(extraction_id)
                sources.append(
                    LineageSourceVO(
                        source_type="heideltime",
                        celex_id=params.celex_id,
                        heideltime_event_id=UUID(extraction_id),
                        description=extraction_texts.get(extraction_id) or None,
                        extraction_method=params.target_fact_name,
                        pipeline=params.pipeline,
                        dspy_module=self.__class__.__name__,
                    )
                )
        return sources


def _count_tokens(text_value: str) -> int:
    """Count prompt tokens with the same encoder as the context guard."""
    return len(_get_tiktoken_enc().encode(text_value))


def _load_digest(sql_store: SQLFactStore, celex_id: str) -> list[EventLine]:
    """Return the digest of the latest ``heideltime_event`` fact, or ``[]``."""
    event_facts = sql_store.select_fact4celex(celex_id, "heideltime_event")
    if not event_facts or not event_facts[-1].value_json:
        return []
    return digest_events(event_facts[-1].value_json)


def _batches(lines: list[EventLine]) -> list[list[EventLine]]:
    """Split a digest into prompt batches under ``HEIDELTIME_AGG_BATCH_TOKENS``."""
    return split_batches(lines, settings.HEIDELTIME_AGG_BATCH_TOKENS, _count_tokens)


def _ask(predictor: dspy.Module, lines: list[EventLine], identifier: str):
    """Send rendered events to ``predictor`` behind the prompt-token guard.

    Raises:
        ValueError: When the rendered prompt exceeds
            ``settings.OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT``.
    """
    prompt = render_digest(lines)
    _check_context_token_limit(prompt, identifier)
    return predictor(heideltime_event_result=prompt).output


def _as_lines(events: list[HeidelTimeCriticalEvent]) -> list[EventLine]:
    """Turn LLM-selected events back into digest lines for a reduce prompt."""
    return [EventLine(date=e.event, actor=e.actor, predicate=e.predicate_object) for e in events]


def _bracket(events: list[HeidelTimeCriticalEvent]) -> list[HeidelTimeCriticalEvent]:
    """Keep only the earliest and the latest validly dated event.

    The conduct step is asked for at most two events per batch, but a model
    that copies the batch back would unbound the period prompt again (canary
    20261104: ~6.5k output tokens per batch, 150s timeouts). The period only
    needs the outer conduct dates, so the cap is enforced here as well.
    """
    dated = [(parsed.first_day, e) for e in events if (parsed := parse_partial_date(e.event))]
    if not dated:
        return []
    dated.sort(key=lambda item: item[0])
    first, last = dated[0][1], dated[-1][1]
    return [first] if first is last else [first, last]


class HeidelTimeEventAggregateModule(dspy.Module):
    """Aggregate ``heideltime_event`` rows into the case's critical events.

    Reads the persisted ``heideltime_event`` fact as a compact digest (real,
    validly dated events, one line each) and asks the LLM for up to 12
    critical events spanning the whole case. A digest above
    ``settings.HEIDELTIME_AGG_BATCH_TOKENS`` is map-reduced: the critical
    events of each date-ordered batch, then one selection over all batch
    candidates, so the earliest conduct and the final outcome both reach the
    last prompt (#186, #224). Every prompt passes the context-token guard.

    Rows formatted for: ``| EVENT | ACTOR | PREDICATE OBJECT |``

    pipeline: heideltime_event_agg
    """

    def __init__(self, sql_store: SQLFactStore) -> None:
        """Inject the fact store and build the aggregation predictor."""
        super().__init__()
        self.sql_store = sql_store
        self.aggregate = dspy.ChainOfThought(HeidelTimeEventAggregateSignature)

    def forward(self, params: FactExtractorParamVO) -> dspy.Prediction:
        """Return the most critical aggregated HeidelTime events for a CELEX document.

        Args:
            params: Extraction parameters; ``celex_id`` selects the events.

        Returns:
            dspy.Prediction: ``output`` holds the chronologically sorted rows,
            empty when the document has no real dated event.

        Raises:
            ValueError: When a prompt exceeds the context-token limit.
        """
        if not params.force_override:
            facts = self.sql_store.select_fact4celex(
                params.celex_id, params.target_fact_name
            )
            if facts and facts[-1].value_json:
                return dspy.Prediction(
                    output=FactOutput(items=facts[-1].value_json, source="sql")
                )

        lines = _load_digest(self.sql_store, params.celex_id)
        if not lines:
            logger.info("No dated heideltime_event rows for CELEX %s.", params.celex_id)
            return dspy.Prediction(output=FactOutput(items=[], source="none"))

        batches = _batches(lines)
        if len(batches) == 1:
            events = _ask(self.aggregate, lines, params.celex_id).events
        else:
            logger.info(
                "heideltime_event_agg %s: map-reduce over %d batches (%d events)",
                params.celex_id, len(batches), len(lines),
            )
            candidates = [
                event
                for batch in batches
                for event in _ask(self.aggregate, batch, params.celex_id).events
            ]
            events = _ask(self.aggregate, _as_lines(candidates), params.celex_id).events

        items = [
            {
                "event": ev.event,
                "actor": ev.actor,
                "predicate object": ev.predicate_object,
            }
            for ev in events
        ]
        items.sort(key=self._event_sort_key)

        return dspy.Prediction(
            output=FactOutput(items=items, source="heideltime_event_agg")
        )

    @staticmethod
    def _event_sort_key(item: dict[str, str]) -> tuple[int, str]:
        """Sort ISO-like event dates chronologically, keeping unknown dates last."""
        event = str(item.get("event", "")).strip()
        match = re.match(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", event)
        if not match:
            return (1, event)

        year, month, day = match.groups()
        normalized = f"{year}-{month or '00'}-{day or '00'}"
        return (0, normalized)


# HeidelTimeEvent_aggregateModule = HeidelTimeEventAggregateModule


class HeideltimeCartelPeriodModule(dspy.Module):
    """DSPy pipeline to extract the cartel period from heideltime_event.

    Reads the persisted ``heideltime_event`` fact as a compact digest and asks
    for the infringement's global start and end. A digest above
    ``settings.HEIDELTIME_AGG_BATCH_TOKENS`` is map-reduced: each date-ordered
    batch is reduced to its earliest and latest conduct event, and the period
    is decided over those alone, so Commission and court steps -- the dates the model latched
    onto as the "end" of oversized documents (#188) -- never reach the period
    prompt. Every prompt passes the context-token guard.

    When the events yield no period, a follow-up (amending / re-adopting)
    decision inherits the period of the original decision of its case via the
    injected :class:`PeriodInheritor`; the borrowed value is linked
    ``derived_from`` the donor's fact, so the evidence verifier treats it as a
    derived fact.

    pipeline: heideltime_cartel_period
    """

    def __init__(self, sql_store: SQLFactStore, inheritor: PeriodInheritor) -> None:
        """Inject the stores and build the conduct and period predictors.

        Args:
            sql_store: Reads the persisted ``heideltime_event`` digest.
            inheritor: Resolves the original decision's period for a
                follow-up decision without one of its own.
        """
        super().__init__()
        self.sql_store = sql_store
        self.inheritor = inheritor
        self.select_conduct = dspy.ChainOfThought(HeideltimeConductEventsSignature)
        self.extract_period = dspy.ChainOfThought(HeideltimeCartelPeriodSignature)

    def forward(self, params: FactExtractorParamVO) -> dspy.Prediction:
        """Return the cartel period of a CELEX document as ``START : END``.

        Args:
            params: Extraction parameters; ``celex_id`` selects the events.

        Returns:
            dspy.Prediction: ``output`` holds one ``date_range`` row, or no
            rows when neither the events nor an original decision of the
            same case yield a period.

        Raises:
            ValueError: When a prompt exceeds the context-token limit.
        """
        if not params.force_override:
            facts = self.sql_store.select_fact4celex(
                params.celex_id, params.target_fact_name
            )
            if facts and facts[-1].value_json:
                return dspy.Prediction(
                    output=FactOutput(items=facts[-1].value_json, source="sql")
                )

        lines = _load_digest(self.sql_store, params.celex_id)
        batches = _batches(lines)
        if len(batches) > 1:
            logger.info(
                "heideltime_cartel_period %s: conduct selection over %d batches (%d events)",
                params.celex_id, len(batches), len(lines),
            )
            lines = _as_lines(
                [
                    event
                    for batch in batches
                    for event in _bracket(_ask(self.select_conduct, batch, params.celex_id).events)
                ]
            )
        if not lines:
            logger.info("No dated conduct events for CELEX %s.", params.celex_id)
            return self._inherit(params)

        date_range = _ask(self.extract_period, lines, params.celex_id).date_range
        if not date_range:
            logger.info(
                "No cartel period date range extracted for CELEX %s.", params.celex_id
            )
            return self._inherit(params)

        return dspy.Prediction(
            output=FactOutput(
                items=[{"date_range": date_range}],
                source="heideltime_cartel_period",
            )
        )

    def _inherit(self, params: FactExtractorParamVO) -> dspy.Prediction:
        """Return the original decision's period, or the empty answer.

        Args:
            params: Extraction parameters of the decision without a period.

        Returns:
            dspy.Prediction: one inherited ``date_range`` row with a
            ``derived_from`` lineage source per donor, or no rows.
        """
        inherited = self.inheritor.find(params.celex_id, params.target_fact_name)
        if inherited is None:
            return dspy.Prediction(output=FactOutput(items=[], source="none"))
        return dspy.Prediction(
            output=FactOutput(
                items=[{"date_range": inherited.date_range}],
                source=INHERITANCE_METHOD,
                lineage_sources=self._donor_lineage(params, inherited),
            )
        )

    def _donor_lineage(
        self, params: FactExtractorParamVO, inherited: InheritedPeriod
    ) -> list[LineageSourceVO]:
        """Cite each donor's period fact as the source of the inherited value.

        Args:
            params: Extraction parameters supplying the fact name and pipeline.
            inherited: The chosen period and its donors.

        Returns:
            One ``fact`` source per donor; a union is linked at the lower
            ``settings.PERIOD_INHERITANCE_UNION_TRUST_RANK``.
        """
        trust_rank = (
            settings.PERIOD_INHERITANCE_UNION_TRUST_RANK
            if inherited.basis is InheritanceBasis.SIBLING_UNION
            else None
        )
        return [
            LineageSourceVO(
                source_type="fact",
                celex_id=donor.celex_id,
                source_fact_id=donor.fact_id,
                description=f"fact:{params.target_fact_name}",
                extraction_method=INHERITANCE_METHOD,
                relation_type="derived_from",
                pipeline=params.pipeline,
                dspy_module=self.__class__.__name__,
                trust_rank=trust_rank,
            )
            for donor in inherited.donors
        ]
