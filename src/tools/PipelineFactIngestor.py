"""
Fact ingestion orchestrator — delegates persistence to ``FactRepository``.

Responsibility:
  - Format extracted content (dict/list/string) into markdown + JSON.
  - Persist via ``FactRepository.upsert_fact()`` (atomic upsert with self-healing dedup).
  - Optionally record data lineage via ``LineageRepository`` (Option 2 / Option 3).
  - Record ingestion audit trail entries.
"""

import logging
import json
import datetime
import re
import uuid
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from pipelines.rag.pgvector.vector_config import DATABASE_URL
from pipelines.rag.pgvector.chunk_repository import ChunkRepository
from pipelines.rag.pgvector.lineage_repository import LineageRepository
from pipelines.rag.pgvector.models import CelexFactIngestionAuditTrail
from pipelines.rag.pgvector.fact_repository import FactRepository
from tools.markdown_utils import format_fact_to_markdown

logger = logging.getLogger(__name__)

# Per-source-type required fields enforced by LineageSourceVO. A source is only
# useful for lineage if its natural key is present.
_REQUIRED_SOURCE_FIELDS: dict[str, tuple[str, ...]] = {
    "chunk": ("celex_id", "chunk_id"),
    "book_case": ("celex_id", "chunk_id"),
    "heideltime": ("celex_id", "heideltime_event_id"),
    "csv_human": ("celex_id", "csv_row"),
    "lei_corporate": ("lei_id",),
    "fact": ("celex_id", "source_fact_id"),
}


def _is_gate_predecessor(predecessor_name: str, condition: str | None) -> bool:
    """Return ``True`` when a predecessor is a control-flow gate, not a source.

    A YAML ``predecessor`` is a *gate* when the fact's ``condition`` tests it as
    a boolean precondition — ``<name> == true`` or ``exists("<name>")`` — which
    decides *whether* the fact is extracted, not *what* its value is (e.g.
    ``court_list_raw`` is gated on ``is_court_involved == true``).  It is a
    genuine *data source* when its value is consumed (e.g.
    ``'court' in court_list_raw``) or when there is no condition at all.

    The discriminator is deliberately the *shape of the condition* rather than
    "does the fact have its own chunk evidence": both ``court_list_raw`` and
    ``court_list`` name their predecessor in ``condition``, so only the per-
    predecessor test separates the gate from the transform.

    Parameters
    ----------
    predecessor_name:
        Name of the predecessor fact declared under ``predecessors``.
    condition:
        The fact's YAML ``condition`` expression, or ``None``.

    Returns
    -------
    bool
        ``True`` if the predecessor is a gate (record as ``gated_by``).
    """
    if not condition:
        return False
    name = re.escape(predecessor_name)
    boolean_test = re.search(rf"\b{name}\s*==\s*true\b", condition, re.IGNORECASE)
    exists_test = re.search(
        rf"""exists\(\s*["']?{name}["']?\s*\)""", condition, re.IGNORECASE
    )
    return bool(boolean_test or exists_test)


class LineageSourceVO(BaseModel):
    """Value object describing one piece of evidence that supports a fact.

    Pass a list of these to ``FactIngestor.save_fact()`` to record data
    lineage in ``celex_facts.fact_source_registry`` and ``celex_facts.fact_source_link``.

    Attributes
    ----------
    source_type:
        Category of the evidence source.
    celex_id:
        CELEX document identifier (required for chunk, book_case, heideltime,
        csv_human sources).
    chunk_id:
        Stable UUID of the chunk row in ``pg_embedding_qwen`` or
        ``pg_embedding_book_qwen``.
    chunk_seq:
        List of ``doc_pos`` values (for display / backfill).
    heideltime_event_id:
        UUID of the ``heideltime_extractions`` row.
    lei_id:
        Legal Entity Identifier (for ``lei_corporate`` sources).
    csv_row:
        Row number in the CSV export (for ``csv_human`` sources).
    source_fact_id:
        UUID of the predecessor ``celex_facts.facts`` row (for ``fact``
        sources), i.e. the fact this one was derived from or gated on.
    description:
        Human-readable label stored on the shared registry row, identifying
        *the source* (e.g. ``"fact:court_list_raw"``).  ``None`` leaves any
        existing label untouched.
    extraction_method:
        Name of the pipeline or module that produced this link.
    pipeline:
        Producing pipeline name (e.g. ``"universal"``), mirroring the audit
        trail.  ``None`` when unknown.
    dspy_module:
        Producing DSPy module class name.  ``None`` when unknown.
    trust_rank:
        Evidence quality 1–10.  ``None`` uses the source-type default.
    relation_type:
        Option 3 edge semantic (default: ``"extracted_from"``).
    conflict_flag:
        ``True`` if this source contradicts another source for the same fact.
    conflict_note:
        Human-readable explanation of the conflict.
    """

    source_type: Literal[
        "chunk", "csv_human", "book_case", "lei_corporate", "heideltime", "fact"
    ]
    celex_id: str | None = Field(default=None)
    chunk_id: str | None = Field(default=None)
    chunk_seq: list[int] | None = Field(default=None)
    heideltime_event_id: UUID | None = Field(default=None)
    lei_id: str | None = Field(default=None)
    csv_row: int | None = Field(default=None)
    source_fact_id: UUID | None = Field(default=None)
    description: str | None = Field(default=None)
    extraction_method: str
    pipeline: str | None = Field(default=None)
    dspy_module: str | None = Field(default=None)
    trust_rank: int | None = Field(default=None)
    relation_type: str = Field(default="extracted_from")
    conflict_flag: bool = Field(default=False)
    conflict_note: str | None = Field(default=None)

    @model_validator(mode="after")
    def _require_natural_key_fields(self) -> "LineageSourceVO":
        """Ensure the fields required by ``source_type`` are populated.

        Raises
        ------
        ValueError
            If a field required for the declared ``source_type`` is missing.
        """
        missing = [
            field
            for field in _REQUIRED_SOURCE_FIELDS[self.source_type]
            if getattr(self, field) is None
        ]
        if missing:
            raise ValueError(
                f"source_type '{self.source_type}' requires {missing} to be set"
            )
        return self


class FactIngestor:
    """Orchestrator that formats, persists, and optionally records lineage for a fact.

    Parameters
    ----------
    target_fact_name:
        The fact name being extracted (e.g. ``"fines"``).
    db_url:
        PostgreSQL connection string.
    fact_table:
        Pre-built ``FactRepository`` instance (injected in tests / batch jobs).
    lineage_repo:
        Optional ``LineageRepository`` instance.  When provided, ``save_fact``
        will persist lineage sources to ``fact_source_registry`` / ``fact_source_link``.
    chunk_repo:
        Optional ``ChunkRepository`` used to resolve ``references_doc`` doc_pos
        values to stable chunk UUIDs when lineage is derived automatically.
        Defaults to a fresh instance when ``lineage_repo`` is supplied.
    """

    def __init__(
        self,
        target_fact_name: str,
        db_url: str = DATABASE_URL,
        fact_table: FactRepository | None = None,
        lineage_repo: LineageRepository | None = None,
        chunk_repo: ChunkRepository | None = None,
    ) -> None:
        self._repo = fact_table if fact_table else FactRepository(db_url)
        self.Session = self._repo.Session
        self.target_fact_name = target_fact_name
        self._lineage_repo = lineage_repo
        self._chunk_repo = chunk_repo if chunk_repo is not None else (
            ChunkRepository() if lineage_repo is not None else None
        )
        # Backward compat alias
        self.fact_table = self._repo

    def exists(self, celex_id: str) -> bool:
        """Check if a fact already exists to prevent duplicate processing."""
        facts = self._repo.select_fact4celex(celex_id, self.target_fact_name)
        return len(facts) > 0

    def save_fact(
        self,
        celex_id: str,
        generated_content: str | list[Any] | dict[str, Any],
        references_doc: list[int] | None = None,
        lineage_sources: list[LineageSourceVO] | None = None,
        pipeline: str | None = None,
        dspy_module: str | None = None,
        predecessors: list[str] | None = None,
        condition: str | None = None,
    ):
        """Save extracted content as a fact and optionally record its lineage.

        Persists the fact via ``FactRepository.upsert_fact()`` with atomic
        dedup semantics.  When ``lineage_sources`` are provided and a
        ``LineageRepository`` was injected at construction time, each source is
        registered in ``fact_source_registry`` and linked via ``fact_source_link``.

        Parameters
        ----------
        celex_id:
            CELEX document identifier.
        generated_content:
            Extracted value as dict, list, or string.
        references_doc:
            Legacy list of ``doc_pos`` integers stored in ``references_doc_json``
            for backward compatibility.
        lineage_sources:
            Optional list of :class:`LineageSourceVO` instances.  Requires a
            ``LineageRepository`` passed at construction; silently skipped
            otherwise.  When omitted and a ``LineageRepository`` is present,
            chunk lineage is derived automatically from ``references_doc``.
        pipeline:
            Producing pipeline name (e.g. ``"universal"``) recorded on each
            ``fact_source_link`` edge.  Applied to auto-derived chunk lineage.
        dspy_module:
            Producing DSPy module class name recorded on each edge.
        predecessors:
            Names of the facts this one was computed from, as declared under
            ``predecessors`` in ``FACT_EXTRACTOR.yaml``.  Each is resolved to
            its ``facts`` row and recorded as a ``fact`` source, *in addition*
            to any chunk lineage.
        condition:
            The fact's YAML ``condition`` expression.  Classifies each
            predecessor as a data source (``derived_from``) or a control-flow
            gate (``gated_by``) — see :func:`_is_gate_predecessor`.

        Returns
        -------
        UUID
            The surrogate PK of the persisted fact row.
        """
        # Format outputs into markdown and JSON
        formatted_md = format_fact_to_markdown(generated_content)
        merged_json = json.dumps(generated_content)

        # Check if the output is empty or an empty JSON list
        if not formatted_md.strip() or merged_json == "[]":
            logger.info(
                "Mapping empty output to 'NA' for %s fact '%s'. (MD: '%s', JSON: '%s')",
                celex_id, self.target_fact_name, formatted_md, merged_json,
            )
            formatted_md = "NA"

        # When column is JSONB, we can pass python list directly instead of stringified json
        references_doc_json = references_doc if references_doc else None

        fact_id = self._repo.upsert_fact(
            celex_id=celex_id,
            name=self.target_fact_name,
            value_md=formatted_md,
            value_json=merged_json,
            references_doc_json=references_doc_json,
        )

        if self._lineage_repo is not None:
            # Chunk evidence and predecessor facts are complementary, not
            # alternatives: a derived fact can cite its own chunks *and* the
            # facts it was computed from, so the two lists are concatenated.
            effective_sources = list(
                lineage_sources
                if lineage_sources is not None
                else self._derive_chunk_lineage(
                    celex_id, references_doc, pipeline, dspy_module
                )
            )
            effective_sources += self._derive_predecessor_lineage(
                celex_id, predecessors, condition, pipeline, dspy_module
            )
            if effective_sources:
                self._persist_lineage(fact_id, effective_sources)

        return fact_id

    def _derive_predecessor_lineage(
        self,
        celex_id: str,
        predecessors: list[str] | None,
        condition: str | None = None,
        pipeline: str | None = None,
        dspy_module: str | None = None,
    ) -> list[LineageSourceVO]:
        """Build ``fact`` lineage sources from the YAML ``predecessors`` list.

        A fact computed from other facts (e.g. ``court_list`` from
        ``court_list_raw``) has no chunk evidence of its own, so each
        predecessor is catalogued as a ``fact`` source and linked with
        ``derived_from`` — or ``gated_by`` when the predecessor is only a
        control-flow precondition (see :func:`_is_gate_predecessor`).

        Parameters
        ----------
        celex_id:
            CELEX document identifier scoping the predecessor lookup.
        predecessors:
            Predecessor fact names from ``FACT_EXTRACTOR.yaml``, or ``None``.
        condition:
            The fact's YAML ``condition``, used to classify each predecessor.
        pipeline:
            Producing pipeline name recorded on each derived source.
        dspy_module:
            Producing DSPy module class name recorded on each derived source.

        Returns
        -------
        list[LineageSourceVO]
            One ``fact`` source per resolved predecessor.  Empty when there are
            no predecessors or none of them exist for this document.
        """
        if not predecessors:
            return []
        sources: list[LineageSourceVO] = []
        for predecessor_name in predecessors:
            rows = self._repo.select_fact4celex(celex_id, predecessor_name)
            if not rows:
                logger.debug(
                    "No '%s' fact for %s — cannot record it as a source of '%s'",
                    predecessor_name, celex_id, self.target_fact_name,
                )
                continue
            is_gate = _is_gate_predecessor(predecessor_name, condition)
            sources.append(
                LineageSourceVO(
                    source_type="fact",
                    celex_id=celex_id,
                    source_fact_id=rows[-1].id,
                    description=f"fact:{predecessor_name}",
                    extraction_method=self.target_fact_name,
                    relation_type="gated_by" if is_gate else "derived_from",
                    pipeline=pipeline,
                    dspy_module=dspy_module,
                )
            )
        return sources

    def _derive_chunk_lineage(
        self,
        celex_id: str,
        references_doc: list[int] | None,
        pipeline: str | None = None,
        dspy_module: str | None = None,
    ) -> list[LineageSourceVO]:
        """Build chunk lineage sources from legacy ``references_doc`` positions.

        Resolves each ``doc_pos`` to its stable ``pg_embedding_qwen`` chunk UUID
        so lineage can be recorded as a byproduct of extraction without the
        DSPy modules emitting :class:`LineageSourceVO` directly.

        Parameters
        ----------
        celex_id:
            CELEX document identifier.
        references_doc:
            List of ``doc_pos`` integers, or ``None``.
        pipeline:
            Producing pipeline name recorded on each derived source.
        dspy_module:
            Producing DSPy module class name recorded on each derived source.

        Returns
        -------
        list[LineageSourceVO]
            One ``chunk`` source per resolved chunk.  Empty when there are no
            references or no chunk repository is available.
        """
        if not references_doc or self._chunk_repo is None:
            return []
        resolved = self._chunk_repo.resolve_chunk_ids_by_doc_pos(celex_id, references_doc)
        return [
            LineageSourceVO(
                source_type="chunk",
                celex_id=celex_id,
                chunk_id=row["chunk_id"],
                chunk_seq=[row["doc_pos"]],
                extraction_method=self.target_fact_name,
                pipeline=pipeline,
                dspy_module=dspy_module,
            )
            for row in resolved
        ]

    def _persist_lineage(self, fact_id: UUID, sources: list[LineageSourceVO]) -> None:
        """Register each source and link it to the fact in one transaction.

        All sources for a fact are written within a single session; each source
        is wrapped in a SAVEPOINT so that a single failing source is rolled back
        in isolation without discarding the others.  Lineage failure never
        propagates — the fact itself is already persisted.

        Parameters
        ----------
        fact_id:
            UUID of the persisted ``celex_facts.facts`` row.
        sources:
            List of :class:`LineageSourceVO` instances.
        """
        repo = self._lineage_repo
        try:
            with repo.Session() as session:
                for src in sources:
                    try:
                        with session.begin_nested():
                            source_id = self._register_source(repo, src, session)
                            repo.link_fact_to_source(
                                fact_id=fact_id,
                                source_id=source_id,
                                extraction_method=src.extraction_method,
                                relation_type=src.relation_type,
                                trust_rank=src.trust_rank,
                                conflict_flag=src.conflict_flag,
                                conflict_note=src.conflict_note,
                                source_type=src.source_type,
                                pipeline=src.pipeline,
                                dspy_module=src.dspy_module,
                                session=session,
                            )
                    except Exception:
                        logger.warning(
                            "Skipping lineage source for fact %s (source_type=%s)",
                            fact_id, src.source_type, exc_info=True,
                        )
                session.commit()
        except Exception:
            logger.exception("Failed to persist lineage for fact %s", fact_id)

    @staticmethod
    def _register_source(
        repo: LineageRepository, src: LineageSourceVO, session: Any
    ) -> UUID:
        """Dispatch a :class:`LineageSourceVO` to the matching register_* call."""
        # ``description`` is forwarded on every branch: it is the only
        # human-readable label on the shared registry row, and dropping it
        # leaves provenance readers with a bare UUID.  ``_upsert_source``
        # overwrites an existing label only when a non-``None`` one is passed,
        # so re-running heals blank labels without clobbering good ones.
        match src.source_type:
            case "chunk":
                return repo.register_chunk_source(
                    celex_id=src.celex_id, chunk_id=src.chunk_id,
                    chunk_seq=src.chunk_seq, description=src.description,
                    session=session,
                )
            case "book_case":
                return repo.register_book_source(
                    celex_id=src.celex_id, chunk_id=src.chunk_id,
                    chunk_seq=src.chunk_seq, description=src.description,
                    session=session,
                )
            case "heideltime":
                return repo.register_heideltime_source(
                    celex_id=src.celex_id,
                    heideltime_event_id=src.heideltime_event_id,
                    description=src.description, session=session,
                )
            case "csv_human":
                return repo.register_csv_source(
                    celex_id=src.celex_id, csv_row=src.csv_row,
                    description=src.description, session=session,
                )
            case "lei_corporate":
                return repo.register_lei_source(
                    lei_id=src.lei_id, description=src.description, session=session
                )
            case "fact":
                # The registry row is shared by every fact this predecessor
                # supports (and one predecessor can be a gate for one fact and
                # data for another), so the description names only the source;
                # the edge semantic lives on fact_source_link.relation_type.
                return repo.register_fact_source(
                    celex_id=src.celex_id,
                    source_fact_id=src.source_fact_id,
                    description=src.description,
                    session=session,
                )
        raise ValueError(f"Unknown source_type '{src.source_type}'")

    def audit_run(self, params: Any, fact_id: uuid.UUID | None, dspy_module: str, success: bool, duration_sec: int, errors: str = ""):
        """
        Saves an execution audit log entry to the fact_ingestion_audit_trail table.
        """
        with self.Session() as session:
            try:
                audit = CelexFactIngestionAuditTrail(
                    celex_id=params.celex_id,
                    fact_id=fact_id,
                    target_fact_name=params.target_fact_name,
                    question=params.question,
                    output_schema=params.output_schema,
                    verify_fact_name=params.verify_fact_name,
                    force_override=str(params.force_override),
                    pipeline=getattr(params, "pipeline", None),
                    dspy_module=dspy_module,
                    predecessors=getattr(params, "predecessors", None),
                    success=success,
                    errors=errors,
                    duration=duration_sec,
                    created_at=datetime.datetime.now()
                )
                session.add(audit)
                session.commit()
            except Exception as e:
                session.rollback()
                logger.error(
                    "Failed to save audit trail for %s (fact_id=%r, type=%s): %s",
                    params.celex_id, fact_id, type(fact_id).__name__, e
                )
