"""
Batch script for knowledge ingestion from the extraction queue.

Reads pending rows from ``knowledge_extraction_queue``, processes each
through :class:`KnowledgeIngestModule`, and updates the queue status.

Modelled on :mod:`batch.Main_ingest_generic_facts` — uses
:class:`BatchJobRunner` for CLI, progress bar, and JSONL audit.

Usage (from project root, with .venv activated):

    .venv/bin/python src/batch/Main_ingest_knowledge.py --limit 10
    .venv/bin/python src/batch/Main_ingest_knowledge.py --dry-run
    .venv/bin/python src/batch/Main_ingest_knowledge.py --limit 1
"""

import logging
import uuid

import dspy
from sqlalchemy import text

from config.llm_factory import batch_profile, create_lm
from pipelines.agent_dspy.knowledge_ingest_module import KnowledgeIngestModule
from pipelines.agent_dspy.knowledge_stage_filter import apply_stage_filter
from pipelines.rag.pgvector.knowledge_ingestor import KnowledgeIngestor
from pipelines.rag.pgvector.knowledge_repository import KnowledgeRepository
from pipelines.rag.pgvector.models import KnowledgeExtractionQueue
from pipelines.rag.pgvector.session import Session

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


class KnowledgeIngestionRunner:
    """Reads from ``knowledge_extraction_queue`` and drives
    :class:`KnowledgeIngestModule` for each pending row.

    Follows the same structural pattern as
    :class:`batch.Main_ingest_generic_facts.FactIngestionOrchestrator`.
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # LM configuration
    # ------------------------------------------------------------------

    def _configure_llm(self) -> None:
        """Configure the global DSPy LM from the shared batch profile."""
        dspy.configure(lm=create_lm(batch_profile()))

    # ------------------------------------------------------------------
    # Queue operations
    # ------------------------------------------------------------------

    @staticmethod
    def _reset_stuck_processing(older_than_minutes: int = 30) -> int:
        """Reset queue rows stuck in ``'processing'`` back to ``'pending'``.

        Rows remain in ``'processing'`` when a previous run crashed before
        calling :meth:`_mark_done` or :meth:`_mark_failed`.  Any row that
        has been in ``'processing'`` longer than *older_than_minutes* is
        safe to re-queue.

        Parameters
        ----------
        older_than_minutes:
            Age threshold in minutes.  Rows younger than this are left
            alone — they may belong to a concurrently running worker.

        Returns
        -------
        int
            Number of rows reset to ``'pending'``.
        """
        with Session() as session:
            result = session.execute(
                text(
                    "UPDATE celex_facts.knowledge_extraction_queue "
                    "SET status = 'pending', error_message = NULL "
                    "WHERE status = 'processing' "
                    "  AND created_at < now() - make_interval(mins => :mins)"
                ),
                {"mins": older_than_minutes},
            )
            session.commit()
            return result.rowcount

    @staticmethod
    def _fetch_pending(limit: int) -> list[KnowledgeExtractionQueue]:
        """SELECT … FOR UPDATE SKIP LOCKED pending queue rows.

        Uses raw SQL + ORM read-back so the row lock is held for the
        duration of the session, preventing double-processing by
        concurrent workers.
        """
        with Session() as session:
            rows = session.execute(
                text(
                    "SELECT id FROM celex_facts.knowledge_extraction_queue "
                    "WHERE status = 'pending' "
                    "ORDER BY created_at ASC "
                    "LIMIT :limit "
                    "FOR UPDATE SKIP LOCKED"
                ),
                {"limit": limit},
            ).fetchall()

            if not rows:
                return []

            ids = [row[0] for row in rows]

            # Mark as processing
            session.execute(
                text(
                    "UPDATE celex_facts.knowledge_extraction_queue "
                    "SET status = 'processing' "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": ids},
            )
            session.commit()

            # Re-read full ORM objects
            items = (
                session.query(KnowledgeExtractionQueue)
                .filter(KnowledgeExtractionQueue.id.in_(ids))
                .order_by(KnowledgeExtractionQueue.created_at.asc())
                .all()
            )
            # Expunge so they survive session close
            for item in items:
                session.expunge(item)
            return items

    @staticmethod
    def _parse_fact_ids(raw_fact_ids: list | None) -> list[uuid.UUID]:
        """Convert the queue row's JSONB ``fact_ids`` into UUIDs.

        Parsing is all-or-nothing on purpose.  Silently keeping the subset
        that happens to parse would ground the extracted nuggets on partial
        evidence, which is worse than refusing the row: the grounding edges
        would claim a provenance the agent never had.

        Parameters
        ----------
        raw_fact_ids:
            The JSONB list as read from the queue row, or ``None``.

        Returns
        -------
        list[uuid.UUID]
            Parsed fact UUIDs, empty when *raw_fact_ids* is empty or ``None``.

        Raises
        ------
        ValueError
            When any element is not a well-formed UUID.  ``uuid.UUID``
            signals a non-string element as ``TypeError`` or
            ``AttributeError``; both are normalised to ``ValueError`` so
            callers have a single failure mode to catch.
        """
        parsed: list[uuid.UUID] = []
        for raw_id in raw_fact_ids or []:
            try:
                parsed.append(uuid.UUID(raw_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(
                    f"malformed fact id {raw_id!r}: {exc}"
                ) from exc
        return parsed

    @staticmethod
    def _mark_done(queue_id: uuid.UUID) -> None:
        """Set queue row to ``done`` with a processed timestamp."""
        with Session() as session:
            session.execute(
                text(
                    "UPDATE celex_facts.knowledge_extraction_queue "
                    "SET status = 'done', processed_at = now() "
                    "WHERE id = :id"
                ),
                {"id": queue_id},
            )
            session.commit()

    @staticmethod
    def _mark_failed(queue_id: uuid.UUID, error_msg: str) -> None:
        """Set queue row to ``failed`` with error details."""
        with Session() as session:
            session.execute(
                text(
                    "UPDATE celex_facts.knowledge_extraction_queue "
                    "SET status = 'failed', error_message = :err, processed_at = now() "
                    "WHERE id = :id"
                ),
                {"id": queue_id, "err": error_msg[:2000]},
            )
            session.commit()

    @staticmethod
    def _mark_filtered(queue_id: uuid.UUID, reason: str) -> None:
        """Set queue row to ``done`` and stamp the structural-filter reason.

        Filtered rows are not technical failures — they are intentional
        drops by the Stage-1 structural rejects gate (meta-task prompts,
        control commands, null-result answers, corpus aggregates, ...).
        Marking them ``done`` keeps the queue draining without forever
        re-trying rubbish, while preserving the reason for triage.
        """
        with Session() as session:
            session.execute(
                text(
                    "UPDATE celex_facts.knowledge_extraction_queue "
                    "SET status = 'done', "
                    "    error_message = :err, "
                    "    processed_at = now() "
                    "WHERE id = :id"
                ),
                {"id": queue_id, "err": f"stage_filter: {reason}"},
            )
            session.commit()

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Entry point — configure, fetch, process, report."""
        import argparse

        parser = argparse.ArgumentParser(
            description="Batch-process knowledge extraction queue."
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Max pending queue rows to process. Default 50.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print pending rows but do not process them.",
        )
        parser.add_argument(
            "--no-judge",
            action="store_true",
            help=(
                "Disable the LLM-as-judge quality gate inside the "
                "ingestor. The structural Stage-1 filter still applies. "
                "Default: judge enabled."
            ),
        )
        parser.add_argument(
            "--reset-stuck",
            action="store_true",
            help=(
                "Before fetching, reset any queue rows stuck in "
                "'processing' (older than 30 minutes) back to 'pending'. "
                "Use after a crashed run leaves orphaned rows."
            ),
        )
        args = parser.parse_args()

        if args.reset_stuck:
            reset_count = self._reset_stuck_processing()
            self.logger.info(
                "Reset %d stuck 'processing' row(s) back to 'pending'.",
                reset_count,
            )

        self._configure_llm()

        # Provision pipeline components.  ``judge=True`` lets the ingestor
        # provision its own ``dspy.Predict(KnowHowJudgeSignature)`` against
        # the globally configured LM.
        knowledge_repo = KnowledgeRepository()
        ingestor = KnowledgeIngestor(
            knowledge_repo=knowledge_repo,
            judge=None if args.no_judge else True,
        )
        module = KnowledgeIngestModule(ingestor=ingestor)

        # Fetch pending queue items
        pending = self._fetch_pending(args.limit)
        self.logger.info("Fetched %d pending queue item(s).", len(pending))

        if not pending:
            self.logger.info("Nothing to process.")
            return

        if args.dry_run:
            for item in pending:
                self.logger.info(
                    "[DRY RUN] id=%s prompt=%s",
                    item.id,
                    item.user_prompt[:80],
                )
                # Reset status back to pending
                with Session() as session:
                    session.execute(
                        text(
                            "UPDATE celex_facts.knowledge_extraction_queue "
                            "SET status = 'pending' WHERE id = :id"
                        ),
                        {"id": item.id},
                    )
                    session.commit()
            return

        # Process each queue item
        success_count = 0
        fail_count = 0
        filtered_count = 0

        for queue_item in pending:
            self.logger.info(
                "Processing queue item %s: %s",
                queue_item.id,
                queue_item.user_prompt[:80],
            )

            # Stage-1 structural rejects — safety net for queue rows that
            # bypassed the producer-side filter (older rubbish backlog,
            # external writers, etc.).  A row without fact_ids is rejected
            # here too: the ingestor grounds on facts only, so extraction
            # would be an LLM call that drops every nugget as ungrounded.
            verdict = apply_stage_filter(
                queue_item.user_prompt,
                queue_item.agent_answer,
                fact_ids=queue_item.fact_ids or [],
            )
            if not verdict.kept:
                self.logger.info(
                    "Queue item %s skipped by stage filter (reason=%s)",
                    queue_item.id,
                    verdict.reason,
                )
                self._mark_filtered(queue_item.id, verdict.reason)
                filtered_count += 1
                continue

            # Parse fact_ids / chunk_ids from JSONB.  Malformed ids are a
            # corrupt-row defect, not a transient error: falling through with
            # an empty catalog would spend an extraction call, drop every
            # nugget as ungrounded, and still mark the row 'done' — the row
            # would be consumed with nothing to show for it.  Fail it instead,
            # so it stays visible and can be re-queued once the producer's
            # payload is repaired.
            try:
                fact_ids = self._parse_fact_ids(queue_item.fact_ids)
            except ValueError as exc:
                self.logger.error(
                    "Queue item %s has unparseable fact_ids %r: %s",
                    queue_item.id,
                    queue_item.fact_ids,
                    exc,
                )
                self._mark_failed(queue_item.id, f"unparseable fact_ids: {exc}")
                fail_count += 1
                continue

            chunk_ids = queue_item.chunk_ids or []

            try:
                result = module.forward(
                    user_prompt=queue_item.user_prompt,
                    agent_answer=queue_item.agent_answer,
                    supporting_facts=queue_item.supporting_facts or "",
                    supporting_fact_ids=fact_ids,
                    supporting_chunk_ids=chunk_ids,
                    queue_item_id=queue_item.id,
                )

                if result.errors:
                    self.logger.warning(
                        "Queue item %s: %d ingested, %d errors",
                        queue_item.id,
                        len(result.ingested_items),
                        len(result.errors),
                    )
                    self._mark_failed(
                        queue_item.id,
                        "; ".join(result.errors),
                    )
                    fail_count += 1
                else:
                    self._mark_done(queue_item.id)
                    success_count += 1

            except Exception as exc:
                self.logger.error(
                    "Queue item %s failed: %s", queue_item.id, exc, exc_info=True
                )
                self._mark_failed(queue_item.id, str(exc))
                fail_count += 1

        self.logger.info(
            "Batch complete: %d succeeded, %d filtered, %d failed out of %d.",
            success_count,
            filtered_count,
            fail_count,
            len(pending),
        )


def main() -> None:
    """Entry point for the knowledge-ingestion batch script."""
    runner = KnowledgeIngestionRunner()
    runner.run()


if __name__ == "__main__":
    main()


"""
UPDATE celex_facts.knowledge_extraction_queue
SET status = 'pending', error_message = NULL
WHERE status = 'failed';
Or run --reset-stuck (which only touches 'processing'; 'failed' rows you reset manually when you want a retry).


"""