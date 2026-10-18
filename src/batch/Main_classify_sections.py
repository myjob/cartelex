"""
Repeatable backfill: classify every ``pg_embedding_qwen`` chunk into a
decision ``section_type`` (parties | recitals_facts | legal_assessment |
fines | operative | annex | other).

Strategy per CELEX document (see ``pipelines/legal/section_classifier.py``):

1. Rules — keyword-anchored heading regexes + the operative-part marker;
   an unrecognized heading is a no-match, never a guess.
2. Plausibility gate — rule labels are only trusted when they respect the
   canonical section order and minimum coverage; documents failing the gate
   (typically OCR-ingested with garbled headings) go entirely to the LLM.
3. Forward-fill — sections are contiguous in ``doc_pos`` order, so unlabeled
   chunks inherit the preceding label.
4. LLM fallback — remaining chunks are classified from content by
   ``SectionClassifierModule`` on the local model; chunks the LLM also fails
   on default to ``other``. With ``--skip-llm`` they stay NULL instead
   (visible and re-runnable).

Idempotency
-----------
Chunks with ``section_type IS NOT NULL`` are never rewritten (they still act
as forward-fill anchors), so a re-run only heals gaps. ``--dry-run`` performs
no writes and no LLM calls; chunks that would reach the LLM are reported as
``llm_pending``.

Usage
-----
    PYTHONPATH=src .venv/bin/python src/batch/Main_classify_sections.py
    PYTHONPATH=src .venv/bin/python src/batch/Main_classify_sections.py --dry-run
    PYTHONPATH=src .venv/bin/python src/batch/Main_classify_sections.py --celex-id CELEX-32002D0759
    PYTHONPATH=src .venv/bin/python src/batch/Main_classify_sections.py --skip-llm
"""

import argparse
import logging
import sys
from typing import Any

import dspy

from config.llm_factory import batch_profile, create_lm
from config.ollama_health_gate import (
    ThermalAbort,
    build_thermal_gate,
    stop_on_thermal_abort,
)
from extract.rag.models.chunk_vo import SectionType
from pipelines.legal.section_classifier import (
    SectionClassifierModule,
    classify_chunk_by_rules,
    forward_fill,
    iter_headings,
    rule_labels_plausible,
)
from pipelines.rag.pgvector.chunk_repository import ChunkRepository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("classify_sections")

_LOG_INTERVAL = 25  # log progress every N CELEX documents


def _configure_dspy() -> None:
    """Configure the global DSPy LM for the classifier fallback from the batch profile."""
    dspy.configure(lm=create_lm(batch_profile()), adapter=dspy.JSONAdapter())


def _neighbor_context(
    chunks: list[dict[str, Any]],
    labels: list[SectionType | None],
    index: int,
) -> str:
    """Summarize adjacent chunks' labels/headings for the LLM classifier.

    Parameters
    ----------
    chunks:
        The document's chunks in ``doc_pos`` order.
    labels:
        Current per-chunk labels (post rules/fill) aligned with *chunks*.
    index:
        Position of the chunk being classified.

    Returns
    -------
    str
        A short description of the neighboring chunks; empty when the chunk
        has no neighbors.
    """
    parts: list[str] = []
    for offset, name in ((-1, "previous"), (1, "next")):
        neighbor = index + offset
        if not 0 <= neighbor < len(chunks):
            continue
        label = labels[neighbor]
        headings = iter_headings(chunks[neighbor]["document"])
        description = f"{name} chunk: section={label.value if label else 'unknown'}"
        if headings:
            description += f", heading '{headings[0]}'"
        parts.append(description)
    return "; ".join(parts)


def _llm_classify(
    classifier: SectionClassifierModule,
    chunks: list[dict[str, Any]],
    labels: list[SectionType | None],
    index: int,
) -> SectionType:
    """Classify one chunk with the LLM, defaulting to ``other`` on failure.

    Parameters
    ----------
    classifier:
        The DSPy fallback module (LM already configured globally).
    chunks:
        The document's chunks in ``doc_pos`` order.
    labels:
        Current per-chunk labels aligned with *chunks*.
    index:
        Position of the chunk to classify.

    Returns
    -------
    SectionType
        The LLM verdict, or ``SectionType.OTHER`` when the call or its
        output validation fails.
    """
    try:
        prediction = classifier(
            chunk_text=chunks[index]["document"],
            neighbor_context=_neighbor_context(chunks, labels, index),
        )
        return prediction.output.section_type
    except Exception:
        logger.exception(
            "LLM classification failed for chunk id=%s", chunks[index]["id"]
        )
        return SectionType.OTHER


def _classify_celex(
    repo: ChunkRepository,
    classifier: SectionClassifierModule | None,
    celex_id: str,
    dry_run: bool,
    stats: dict[str, int],
) -> None:
    """Classify and persist section types for one CELEX document.

    Parameters
    ----------
    repo:
        Chunk repository (DB boundary).
    classifier:
        LLM fallback module, or ``None`` when the LLM is skipped
        (``--skip-llm`` or ``--dry-run``).
    celex_id:
        The document to process.
    dry_run:
        When ``True``, nothing is written.
    stats:
        Mutable counters shared across the run.
    """
    chunks = repo.select_chunks_for_sections(celex_id)
    if not chunks:
        return

    # Pre-existing labels are respected (idempotency) and anchor the fill.
    existing = [
        SectionType(c["section_type"]) if c["section_type"] else None for c in chunks
    ]
    stats["skipped_already"] += sum(1 for label in existing if label is not None)

    rule_labels = [
        existing[i]
        if existing[i] is not None
        else classify_chunk_by_rules(chunks[i]["document"], chunks[i]["doc_pos"])
        for i in range(len(chunks))
    ]

    if rule_labels_plausible(rule_labels):
        new_rule_hits = sum(
            1
            for i in range(len(chunks))
            if existing[i] is None and rule_labels[i] is not None
        )
        stats["rule_labeled"] += new_rule_hits
        labels = forward_fill(rule_labels)
        stats["forward_filled"] += sum(
            1
            for i in range(len(chunks))
            if rule_labels[i] is None and labels[i] is not None
        )
    else:
        # Untrustworthy headings (OCR cohort): discard this run's rule labels,
        # keep pre-existing ones, let the LLM judge from content.
        stats["gate_rejected"] += 1
        logger.warning(
            "Plausibility gate rejected rule labels for %s (%d chunks)",
            celex_id,
            len(chunks),
        )
        labels = list(existing)

    for i in range(len(chunks)):
        if existing[i] is not None:
            continue
        label = labels[i]
        if label is None:
            if classifier is None:
                stats["llm_pending"] += 1
                continue
            label = _llm_classify(classifier, chunks, labels, i)
            labels[i] = label
            if label is SectionType.OTHER:
                stats["defaulted_other"] += 1
            else:
                stats["llm_labeled"] += 1
        if dry_run:
            continue
        repo.update_section_type(chunks[i]["id"], label.value)


def run(
    dry_run: bool = False,
    celex_filter: str | None = None,
    skip_llm: bool = False,
    no_thermal_gate: bool = False,
) -> None:
    """Classify sections for all documents with unclassified chunks.

    Parameters
    ----------
    dry_run:
        When ``True``, runs rules/gate/fill but writes nothing and makes no
        LLM calls.
    celex_filter:
        If set, only process this CELEX document.
    skip_llm:
        When ``True``, skip the LLM fallback; unresolved chunks stay NULL.
    no_thermal_gate:
        Skip the DGX health/temperature check before each document. The gate
        only runs when the LLM fallback is live (not dry run, not skip-llm).
    """
    repo = ChunkRepository()
    celex_ids = (
        [celex_filter] if celex_filter else repo.select_celex_ids_missing_section_type()
    )
    logger.info(
        "Classifying sections for %d CELEX document(s)%s",
        len(celex_ids),
        " [DRY-RUN]" if dry_run else "",
    )

    classifier: SectionClassifierModule | None = None
    gate = None
    if not skip_llm and not dry_run:
        _configure_dspy()
        classifier = SectionClassifierModule()
        gate = build_thermal_gate(dry_run=False, disabled=no_thermal_gate)
        if gate is not None:
            gate.clock_controller.ensure_default()

    stats: dict[str, int] = {
        "rule_labeled": 0,
        "forward_filled": 0,
        "gate_rejected": 0,
        "llm_labeled": 0,
        "defaulted_other": 0,
        "llm_pending": 0,
        "skipped_already": 0,
        "errors": 0,
    }

    for idx, celex_id in enumerate(celex_ids, start=1):
        # Outside the try below: an abort is not a per-document failure.
        if gate is not None:
            try:
                gate.check_or_wait()
            except ThermalAbort as abort:
                stop_on_thermal_abort(
                    logger, gate, abort, len(celex_ids) - idx + 1,
                    "re-run this script; classified chunks are never rewritten",
                )
        try:
            _classify_celex(repo, classifier, celex_id, dry_run, stats)
        except Exception:
            logger.exception("Failed to classify sections for %s", celex_id)
            stats["errors"] += 1
        if idx % _LOG_INTERVAL == 0:
            logger.info("Processed %d/%d documents — %s", idx, len(celex_ids), stats)

    logger.info(
        "Section classification complete%s — %s",
        " [DRY-RUN]" if dry_run else "",
        stats,
    )


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Classify pg_embedding_qwen chunks into decision section types"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run rules/gate/fill without writing or calling the LLM",
    )
    parser.add_argument(
        "--celex-id",
        metavar="CELEX_ID",
        default=None,
        help="Restrict classification to a single CELEX document",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Rules-only pass; unresolved chunks stay NULL for a later run",
    )
    parser.add_argument(
        "--no-thermal-gate",
        action="store_true",
        help=(
            "Skip the DGX health/temperature check before each document. The "
            "gate exists because the Ollama box dies silently above ~85C."
        ),
    )
    args = parser.parse_args()
    run(
        dry_run=args.dry_run,
        celex_filter=args.celex_id,
        skip_llm=args.skip_llm,
        no_thermal_gate=args.no_thermal_gate,
    )


if __name__ == "__main__":
    main()
