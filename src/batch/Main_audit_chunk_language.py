"""
Repeatable audit: find the ``pg_embedding_qwen`` documents that were ingested
from a non-English source instead of the English translation.

Such a document is invisible to the English fact extractors and is mis-stemmed
on the lexical retrieval leg (``document_tsv`` is hardcoded to the ``'english'``
FTS configuration), so it needs re-loading from the correct source markdown.

Strategy per CELEX document (see ``pipelines/legal/language_audit.py``):

1. Score every chunk with ``langdetect``. Short, low-confidence and featureless
   chunks are recorded as unscorable and excluded from the evidence base.
2. Aggregate a **character-weighted** non-English share. Detector misfires
   cluster in short chunks, so char-weighting is what distinguishes a
   wrong-language load from an English decision that quotes foreign material.
3. Turn the share into a verdict via the two ``LANGUAGE_AUDIT_*`` thresholds,
   and cross-check it against the descriptor's recorded ``language_init`` /
   ``language_translated``.

The whole corpus is scanned, never a sample: at roughly 3.6 ms per chunk a
56k-chunk corpus takes about 200 seconds, while a 10% sample of a median
75-chunk document is ~7 chunks -- too little to place a document against any
threshold, and it silently misses sparse contamination such as a single
foreign-language annex.

Idempotency
-----------
The audit performs **no** Postgres writes; the only side effect is a new
timestamped CSV under ``settings.REPORT_PATH``, which ``--dry-run`` suppresses.
Verdicts are reproducible run to run because the detector seed is pinned in
``pipelines/legal/language_audit.py``.

Usage
-----
    PYTHONPATH=src .venv/bin/python src/batch/Main_audit_chunk_language.py
    PYTHONPATH=src .venv/bin/python src/batch/Main_audit_chunk_language.py --dry-run
    PYTHONPATH=src .venv/bin/python src/batch/Main_audit_chunk_language.py --celex-id 39452_3336_8
    PYTHONPATH=src .venv/bin/python src/batch/Main_audit_chunk_language.py --limit 25
"""

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from config.settings import settings
from pipelines.legal.language_audit import (
    DocumentLanguageAudit,
    LanguageVerdict,
    aggregate_document_language,
    detect_chunk_language,
    expected_languages_from_descriptors,
)
from pipelines.rag.pgvector.chunk_repository import ChunkRepository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("audit_chunk_language")

_LOG_INTERVAL = 50  # log progress every N CELEX documents

_REPORT_STEM = "language_audit"

_REPORT_COLUMNS = (
    "celex_id",
    "verdict",
    "non_english_char_share",
    "non_english_chunk_share",
    "dominant_non_english",
    "descriptor_agreement",
    "language_init",
    "language_translated",
    "chunks_total",
    "chunks_scored",
    "chunks_too_short",
    "chunks_low_confidence",
    "chunks_no_features",
    "chars_scored",
    "top_languages",
    "worst_doc_positions",
)


def _load_expected_languages() -> dict[str, tuple[str | None, str | None]]:
    """Load the per-document expected languages from the descriptor file.

    A missing or unreadable descriptor file is not fatal: the audit still runs
    and every document is reported with ``descriptor_agreement=no_metadata``.

    Returns
    -------
    dict[str, tuple[str | None, str | None]]
        Maps celex_id to ``(language_init, language_translated)``; empty when
        the descriptor file cannot be read.
    """
    path = Path(settings.LANGUAGE_AUDIT_DESCRIPTOR_PATH)
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "Descriptor file unusable at %s — running without the language cross-check",
            path,
        )
        return {}
    return expected_languages_from_descriptors(payload)


def _audit_celex(
    repo: ChunkRepository,
    celex_id: str,
    expected: dict[str, tuple[str | None, str | None]],
    stats: dict[str, int],
) -> DocumentLanguageAudit | None:
    """Score one CELEX document's chunks and aggregate its verdict.

    Parameters
    ----------
    repo:
        Chunk repository (DB boundary); only read methods are used.
    celex_id:
        The document to audit.
    expected:
        Descriptor languages keyed by celex_id, from ``_load_expected_languages``.
    stats:
        Mutable counters shared across the run.

    Returns
    -------
    DocumentLanguageAudit | None
        The document's verdict, or ``None`` when it has no chunks.
    """
    chunks = repo.select_chunks_for_language_audit(celex_id)
    if not chunks:
        return None

    observations = [
        detect_chunk_language(chunk["document"], chunk["doc_pos"]) for chunk in chunks
    ]
    language_init, language_translated = expected.get(celex_id, (None, None))
    audit = aggregate_document_language(
        celex_id, observations, language_init, language_translated
    )

    stats["documents"] += 1
    stats["chunks"] += len(chunks)
    match audit.verdict:
        case LanguageVerdict.NON_ENGLISH:
            stats["flagged_non_english"] += 1
        case LanguageVerdict.REVIEW:
            stats["flagged_review"] += 1
        case LanguageVerdict.INSUFFICIENT_EVIDENCE:
            stats["insufficient"] += 1
        case _:
            stats["clean"] += 1
    return audit


def _format_top_languages(audit: DocumentLanguageAudit) -> str:
    """Render a document's language tallies as one report cell.

    Parameters
    ----------
    audit:
        The document audit whose tallies are rendered.

    Returns
    -------
    str
        Semicolon-separated ``lang:chunks/chars`` entries, highest volume first.
    """
    return "; ".join(
        f"{tally.language}:{tally.chunks}/{tally.chars}" for tally in audit.top_languages
    )


def _write_report(
    audits: list[DocumentLanguageAudit], output_dir: str | Path
) -> Path | None:
    """Write the per-document audit report as a timestamped CSV.

    Parameters
    ----------
    audits:
        Every audited document, in any order.
    output_dir:
        Directory the report is written to; created if absent.

    Returns
    -------
    Path | None
        The report path, or ``None`` when there was nothing to report.
    """
    if not audits:
        return None

    frame = pl.DataFrame(
        [
            {
                "celex_id": audit.celex_id,
                "verdict": str(audit.verdict),
                "non_english_char_share": round(audit.non_english_char_share, 4),
                "non_english_chunk_share": round(audit.non_english_chunk_share, 4),
                "dominant_non_english": audit.dominant_non_english,
                "descriptor_agreement": str(audit.descriptor_agreement),
                "language_init": audit.language_init,
                "language_translated": audit.language_translated,
                "chunks_total": audit.chunks_total,
                "chunks_scored": audit.chunks_scored,
                "chunks_too_short": audit.chunks_too_short,
                "chunks_low_confidence": audit.chunks_low_confidence,
                "chunks_no_features": audit.chunks_no_features,
                "chars_scored": audit.chars_scored,
                "top_languages": _format_top_languages(audit),
                "worst_doc_positions": ",".join(
                    str(pos) for pos in audit.worst_doc_positions
                ),
            }
            for audit in audits
        ]
    ).select(_REPORT_COLUMNS)

    # Secondary sort by celex_id keeps re-runs byte-identical when shares tie.
    frame = frame.sort(
        ["non_english_char_share", "celex_id"], descending=[True, False]
    )

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    path = directory / f"{_REPORT_STEM}_{timestamp}.csv"
    frame.write_csv(path)
    return path


def _log_verdicts(audits: list[DocumentLanguageAudit]) -> None:
    """Log one line per document that is not plainly English.

    This is the actionable output; the CSV holds the full corpus for context.

    Parameters
    ----------
    audits:
        Every audited document.
    """
    flagged = sorted(
        (a for a in audits if a.verdict is not LanguageVerdict.ENGLISH),
        key=lambda a: -a.non_english_char_share,
    )
    if not flagged:
        logger.info("No document deviates from the expected language")
        return

    logger.info("%d document(s) need attention:", len(flagged))
    for audit in flagged:
        logger.info(
            "  %-24s %-22s share=%.3f dominant=%-4s descriptor=%-24s worst_doc_pos=%s",
            audit.celex_id,
            str(audit.verdict),
            audit.non_english_char_share,
            audit.dominant_non_english or "-",
            str(audit.descriptor_agreement),
            audit.worst_doc_positions or "-",
        )


def run(
    dry_run: bool = False,
    celex_filter: str | None = None,
    limit: int = 0,
) -> None:
    """Audit the ingested language of every document in the chunk table.

    Parameters
    ----------
    dry_run:
        When ``True``, verdicts are logged but no report file is written.
    celex_filter:
        If set, only audit this CELEX document.
    limit:
        If positive, audit at most this many documents (smoke runs).
    """
    repo = ChunkRepository()
    celex_ids = [celex_filter] if celex_filter else repo.select_celex_ids_all()
    if limit > 0:
        celex_ids = celex_ids[:limit]

    logger.info(
        "Auditing chunk language for %d CELEX document(s)%s",
        len(celex_ids),
        " [DRY-RUN]" if dry_run else "",
    )

    expected = _load_expected_languages()
    stats: dict[str, int] = {
        "documents": 0,
        "chunks": 0,
        "clean": 0,
        "flagged_review": 0,
        "flagged_non_english": 0,
        "insufficient": 0,
        "empty": 0,
        "errors": 0,
    }
    audits: list[DocumentLanguageAudit] = []

    for idx, celex_id in enumerate(celex_ids, start=1):
        try:
            audit = _audit_celex(repo, celex_id, expected, stats)
            if audit is None:
                stats["empty"] += 1
            else:
                audits.append(audit)
        except Exception:
            logger.exception("Failed to audit chunk language for %s", celex_id)
            stats["errors"] += 1
        if idx % _LOG_INTERVAL == 0:
            logger.info("Processed %d/%d documents — %s", idx, len(celex_ids), stats)

    _log_verdicts(audits)

    if not dry_run:
        path = _write_report(audits, settings.REPORT_PATH)
        if path is not None:
            logger.info("Report written to %s", path)

    logger.info(
        "Language audit complete%s — %s",
        " [DRY-RUN]" if dry_run else "",
        stats,
    )


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Audit the ingested language of pg_embedding_qwen documents"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log verdicts without writing the CSV report",
    )
    parser.add_argument(
        "--celex-id",
        metavar="CELEX_ID",
        default=None,
        help="Restrict the audit to a single CELEX document",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Audit at most N documents (0 = the whole corpus)",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run, celex_filter=args.celex_id, limit=args.limit)


if __name__ == "__main__":
    main()
