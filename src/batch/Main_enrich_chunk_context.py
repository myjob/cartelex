"""
Repeatable backfill: Anthropic-style contextual retrieval for
``pg_embedding_qwen`` chunks.

For each chunk the local model writes a 1-2 sentence ``context_prefix``
(case name, section, what the chunk covers), and the chunk is re-embedded as
``context_prefix + document``. The re-embed IS the feature: a chunk like
"The fine is set at EUR 30 million" embeds without any case or section
identity, so case-specific queries miss it in the dense leg; baking the
context into the vector is where the published retrieval-failure reduction
comes from. The FTS leg picks the prefix up automatically because
``document_tsv`` is a STORED generated column over
``coalesce(context_prefix, '') || ' ' || document``.

The stored ``document`` column is never modified; only the string sent to the
embedding model is tail-truncated when prefix + document would exceed the
embedder's hard 2048-token cap (``build_embedding_input``).

Run ``Main_classify_sections.py`` first — the chunk's ``section_type`` feeds
the prefix prompt.

Idempotency
-----------
Only ``context_prefix IS NULL`` chunks are fetched; prefix and embedding land
in one transaction per chunk, so an interrupted run resumes exactly where it
stopped. ``--dry-run`` performs no writes, no LLM calls and no embedding
calls.

Scope
-----
Pilot first (decision 2026-07-05): run per ``--celex-id`` on known test cases
and inspect retrieval deltas before any corpus-wide run.

Usage
-----
    PYTHONPATH=src .venv/bin/python src/batch/Main_enrich_chunk_context.py --celex-id CELEX-32002D0759
    PYTHONPATH=src .venv/bin/python src/batch/Main_enrich_chunk_context.py --celex-id CELEX-32002D0759 --dry-run
    PYTHONPATH=src .venv/bin/python src/batch/Main_enrich_chunk_context.py --limit 20
"""

import argparse
import logging
import sys
from typing import Any

import dspy
import tiktoken

from config.llm_factory import batch_profile, create_lm
from config.ollama_health_gate import (
    OllamaHealthGate,
    ThermalAbort,
    build_thermal_gate,
    stop_on_thermal_abort,
)
from config.settings import settings
from pipelines.legal.section_classifier import iter_headings
from pipelines.rag.pgvector.chunk_repository import ChunkRepository
from pipelines.rag.pgvector.embeddings.qwen3_embedding import retrieve_qwen3_embedding

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("enrich_chunk_context")

_LOG_INTERVAL = 50  # log progress every N chunks

# Characters of the doc_pos-0 chunk used as the case identification hint.
_CASE_HINT_CHARS = 500

_EMBED_SEPARATOR = "\n\n"


class ChunkContextSignature(dspy.Signature):
    """Write a short context line that situates a chunk of an EU Commission
    competition decision for search retrieval.

    The line is prepended to the chunk before embedding, so it must pack the
    identifying facts a searcher would query for: the case (parties, cartel/
    market), the decision section, and what this specific chunk covers.
    Output 1-2 plain sentences; no markdown, no quotes, no restating of the
    full chunk.
    """

    case_hint: str = dspy.InputField(
        desc="Opening of the decision's first page: names the case and parties."
    )
    celex_id: str = dspy.InputField(desc="CELEX identifier of the decision.")
    section_label: str = dspy.InputField(
        desc="Classified decision section of the chunk; may be 'unknown'."
    )
    preceding_heading: str = dspy.InputField(
        desc="Nearest markdown heading at or before this chunk; may be empty."
    )
    chunk_text: str = dspy.InputField(desc="The chunk text to contextualize.")
    context_prefix: str = dspy.OutputField(
        desc=(
            "The contextual-retrieval line for this chunk: 1-2 plain "
            "sentences situating it (case parties/subject, decision section, "
            "what the chunk covers). No markdown, no quotes."
        )
    )


class ChunkContextModule(dspy.Module):
    """Generate a chunk's context prefix with the configured local LM.

    Parameters
    ----------
    predictor:
        Optional DSPy-compatible callable injected for tests; defaults to
        ``dspy.Predict(ChunkContextSignature)``.
    """

    def __init__(self, predictor: Any | None = None) -> None:
        super().__init__()
        self.contextualize = predictor or dspy.Predict(ChunkContextSignature)

    def forward(
        self,
        case_hint: str,
        celex_id: str,
        section_label: str,
        preceding_heading: str,
        chunk_text: str,
    ) -> dspy.Prediction:
        """Produce a chunk's ``context_prefix`` line.

        Parameters
        ----------
        case_hint:
            Opening of the decision's first page.
        celex_id:
            CELEX identifier of the decision.
        section_label:
            Classified section of the chunk, or ``"unknown"``.
        preceding_heading:
            Nearest markdown heading at or before the chunk; may be empty.
        chunk_text:
            The chunk text to contextualize.

        Returns
        -------
        dspy.Prediction
            Prediction whose ``context_prefix`` is the stripped context line.
        """
        prediction = self.contextualize(
            case_hint=case_hint,
            celex_id=celex_id,
            section_label=section_label,
            preceding_heading=preceding_heading,
            chunk_text=chunk_text,
        )
        return dspy.Prediction(context_prefix=prediction.context_prefix.strip())


def build_embedding_input(
    context_prefix: str, document: str, token_limit: int
) -> str:
    """Compose the embedder input, tail-truncating the document copy to fit.

    The Qwen3 embedder hard-fails above its token cap and chunks are already
    token-targeted near that budget, so prepending even a two-sentence prefix
    can push some chunks over the limit. This helper keeps the FULL prefix
    (the retrieval signal being added) and cuts only as many tokens as needed
    from the end of the document copy sent to the embedder. The stored
    ``document`` and ``context_prefix`` columns are never affected. Tokens
    are counted with tiktoken cl100k — the same tokenizer as the embedder's
    guard in ``qwen3_embedding.py``.

    Parameters
    ----------
    context_prefix:
        The generated context line; may be empty.
    document:
        The chunk text.
    token_limit:
        Maximum tokens the embedder accepts
        (``settings.OLLAMA_EMBEDDING_QWEN3_TOKEN_LIMIT``).

    Returns
    -------
    str
        ``context_prefix + separator + document``, with the document tail
        truncated only when the total would exceed *token_limit*.
    """
    encoding = tiktoken.get_encoding("cl100k_base")
    combined = (
        f"{context_prefix}{_EMBED_SEPARATOR}{document}" if context_prefix else document
    )
    if len(encoding.encode(combined)) <= token_limit:
        return combined

    prefix_part = f"{context_prefix}{_EMBED_SEPARATOR}" if context_prefix else ""
    document_budget = token_limit - len(encoding.encode(prefix_part))
    if document_budget <= 0:
        # Pathological oversized prefix: fall back to the bare document.
        return encoding.decode(encoding.encode(document)[:token_limit])
    truncated_document = encoding.decode(encoding.encode(document)[:document_budget])
    return f"{prefix_part}{truncated_document}"


def _configure_dspy() -> None:
    """Configure the global DSPy LM for prefix generation from the batch profile."""
    dspy.configure(lm=create_lm(batch_profile()), adapter=dspy.JSONAdapter())


def _enrich_celex(
    repo: ChunkRepository,
    module: ChunkContextModule | None,
    celex_id: str,
    dry_run: bool,
    limit: int | None,
    stats: dict[str, int],
    gate: OllamaHealthGate | None = None,
) -> None:
    """Generate, embed and persist context prefixes for one CELEX document.

    Iterates ALL chunks in ``doc_pos`` order so the nearest preceding heading
    is tracked across already-enriched chunks, but only ``context_prefix IS
    NULL`` chunks are written.

    Parameters
    ----------
    repo:
        Chunk repository (DB boundary).
    module:
        Prefix-generation module; ``None`` only in dry-run mode.
    celex_id:
        The document to process.
    dry_run:
        When ``True``, nothing is generated, embedded or written.
    limit:
        Optional cap on total enriched chunks across the run (smoke tests).
    stats:
        Mutable counters shared across the run.
    gate:
        Thermal gate checked before every LLM call (this is the highest
        call-rate batch in the repo); ``None`` when the run is ungated.

    Raises
    ------
    ThermalAbort
        Propagated from the gate so the run stops on a clean chunk boundary.
    """
    missing_ids = {c["id"] for c in repo.select_chunks_missing_context(celex_id)}
    if not missing_ids:
        return
    chunks = repo.select_chunks_for_sections(celex_id)
    case_hint = (repo.select_first_page(celex_id) or "")[:_CASE_HINT_CHARS]
    token_limit = int(settings.OLLAMA_EMBEDDING_QWEN3_TOKEN_LIMIT)

    preceding_heading = ""
    for chunk in chunks:
        headings = iter_headings(chunk["document"])
        if headings:
            preceding_heading = headings[-1]
        if chunk["id"] not in missing_ids:
            stats["skipped_existing"] += 1
            continue
        if limit is not None and stats["enriched"] >= limit:
            return
        if dry_run:
            stats["enriched"] += 1
            continue
        # Outside the try below: an abort is not a per-chunk failure.
        if gate is not None:
            gate.check_or_wait()
        try:
            prediction = module(
                case_hint=case_hint,
                celex_id=celex_id,
                section_label=chunk["section_type"] or "unknown",
                preceding_heading=preceding_heading,
                chunk_text=chunk["document"],
            )
            context_prefix = prediction.context_prefix.strip()
            if not context_prefix:
                raise ValueError("empty context_prefix from LM")
            embedding_input = build_embedding_input(
                context_prefix, chunk["document"], token_limit
            )
            embedding = retrieve_qwen3_embedding(embedding_input)
            repo.update_chunk_context(chunk["id"], context_prefix, embedding)
            stats["enriched"] += 1
        except Exception:
            logger.exception("Enrichment failed for chunk id=%s", chunk["id"])
            stats["errors"] += 1
        if stats["enriched"] and stats["enriched"] % _LOG_INTERVAL == 0:
            logger.info("Progress — %s", stats)


def run(
    dry_run: bool = False,
    celex_filter: str | None = None,
    limit: int | None = None,
    no_thermal_gate: bool = False,
) -> None:
    """Enrich chunks with context prefixes and re-embed them.

    Parameters
    ----------
    dry_run:
        When ``True``, counts the would-be work without LLM/embedding calls
        or writes.
    celex_filter:
        If set, only process this CELEX document (the pilot mode).
    limit:
        Optional cap on total enriched chunks (smoke tests).
    no_thermal_gate:
        Skip the DGX health/temperature check before each chunk.
    """
    repo = ChunkRepository()
    celex_ids = (
        [celex_filter] if celex_filter else repo.select_celex_ids_missing_context()
    )
    logger.info(
        "Enriching chunk context for %d CELEX document(s)%s",
        len(celex_ids),
        " [DRY-RUN]" if dry_run else "",
    )

    module: ChunkContextModule | None = None
    gate = build_thermal_gate(dry_run, no_thermal_gate)
    if not dry_run:
        _configure_dspy()
        module = ChunkContextModule()
    if gate is not None:
        gate.clock_controller.ensure_default()

    stats: dict[str, int] = {"enriched": 0, "skipped_existing": 0, "errors": 0}

    for idx, celex_id in enumerate(celex_ids, start=1):
        if limit is not None and stats["enriched"] >= limit:
            break
        try:
            _enrich_celex(repo, module, celex_id, dry_run, limit, stats, gate)
        except ThermalAbort as abort:
            stop_on_thermal_abort(
                logger, gate, abort, len(celex_ids) - idx + 1,
                "re-run this script; enriched chunks are skipped automatically",
            )
        except Exception:
            logger.exception("Failed to enrich %s", celex_id)
            stats["errors"] += 1

    logger.info(
        "Context enrichment complete%s — %s",
        " [DRY-RUN]" if dry_run else "",
        stats,
    )


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Generate contextual-retrieval prefixes and re-embed chunks"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count the would-be work without LLM/embedding calls or writes",
    )
    parser.add_argument(
        "--celex-id",
        metavar="CELEX_ID",
        default=None,
        help="Restrict enrichment to a single CELEX document (pilot mode)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of enriched chunks (smoke tests)",
    )
    parser.add_argument(
        "--no-thermal-gate",
        action="store_true",
        help=(
            "Skip the DGX health/temperature check before each chunk. The gate "
            "exists because the Ollama box dies silently above ~85C."
        ),
    )
    args = parser.parse_args()
    run(
        dry_run=args.dry_run,
        celex_filter=args.celex_id,
        limit=args.limit,
        no_thermal_gate=args.no_thermal_gate,
    )


if __name__ == "__main__":
    main()
