"""
Qwen3-based embedding ingestion for question–fact pairs.

Reads rows from the ``celex_facts.question2fact`` view, generates 4096-dim
embeddings via the local Ollama Qwen3 model, and upserts them into
``celex_facts.question2fact2embeddings``.

Classes:
    FactEmbeddingLoaderQwen: embeds a single (celex_id, fact_name) pair.
    AllFactsEmbeddingLoaderQwen: iterates all facts and delegates to
        FactEmbeddingLoaderQwen for each unique (celex_id, name) pair.
"""

import hashlib
import logging
import math
import re
from typing import Callable

from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session as SessionType

from pipelines.rag.pgvector.embeddings.qwen3_embedding import (
    count_tokens,
    embedding_token_limit,
    retrieve_qwen3_embedding,
)
from pipelines.rag.pgvector.session import Session

logger = logging.getLogger(__name__)

# Tokenizer used for the size guard (tiktoken cl100k_base) only approximates the
# Qwen3 tokenizer, so windows are sized below the limit by this margin.
_TOKEN_WINDOW_MARGIN = 64

# Model name whose tiktoken encoding the window splitter uses. Matches the
# encoder behind count_tokens() and TokenTargetMarkdownSplitter.
_SPLITTER_MODEL_NAME = "gpt-3.5-turbo"

_UPSERT_SQL = """
INSERT INTO celex_facts.question2fact2embeddings
    (fk_fact_id, celex_id, question, fact_name, value_txt, embeddings, hash_q_a, ingestion_timestamp)
VALUES
    (:fk_fact_id, :celex_id, :question, :fact_name, :value_txt,
     cast(:vec AS vector), :hash_q_a, now())
ON CONFLICT (fk_fact_id) DO UPDATE SET
    embeddings          = EXCLUDED.embeddings,
    hash_q_a            = EXCLUDED.hash_q_a,
    ingestion_timestamp = now()
"""

_SELECT_FACTS_SQL = """
SELECT id, question, fact_name, value_md
FROM celex_facts.question2fact
WHERE celex_id = :celex_id
  AND fact_name = :fact_name
"""


class FactEmbeddingLoaderQwen:
    """
    Embeds question–fact pairs and persists them to question2fact2embeddings.

    Follows the Provisioner pattern: the SQLAlchemy session factory is injected
    at construction time so callers can substitute a test double without touching
    production DB credentials.

    Args:
        session_factory: A callable returning a SQLAlchemy ``Session``. Defaults
            to the project-wide ``Session`` from ``pipelines.rag.pgvector.session``.
    """

    def __init__(
        self, session_factory: Callable[[], SessionType] | None = None
    ) -> None:
        """Initialise the loader with an optional session factory override."""
        self._session_factory: Callable[[], SessionType] = session_factory or Session

    @staticmethod
    def _clean_for_embedding(md: str) -> str:
        """
        Remove markdown artefacts that add noise to embeddings.

        Strips table dividers, fenced code blocks, inline images, and collapses
        runs of whitespace to a single space.

        Args:
            md: Raw markdown text.

        Returns:
            Cleaned plain-text suitable for embedding.
        """
        md = re.sub(r"\|[-:]+\|[-|\s:]+", "", md)  # table dividers
        md = re.sub(r"```.*?```", "", md, flags=re.DOTALL)  # code blocks
        md = re.sub(r"!\[.*?\]\(.*?\)", "", md)  # images
        md = re.sub(r"\s+", " ", md).strip()
        return md

    @staticmethod
    def _compute_hash(passage_text: str) -> str:
        """
        Compute an MD5 fingerprint of the full passage text.

        Used as ``hash_q_a`` in the target table to detect when the underlying
        fact value has changed and the embedding must be refreshed.

        Args:
            passage_text: The complete passage string passed to the embedding model.

        Returns:
            Hexadecimal MD5 digest string.
        """
        return hashlib.md5(passage_text.encode()).hexdigest()

    @staticmethod
    def _mean_pool(vectors: list[list[float]]) -> list[float]:
        """
        Combine several window embeddings into one unit-length vector.

        Each vector is L2-normalised before averaging so that no window
        dominates by magnitude, and the mean is re-normalised because the
        consumer ranks with cosine distance (``<=>``).

        Args:
            vectors: Non-empty list of equal-length embedding vectors.

        Returns:
            The pooled, unit-length vector. Degenerate all-zero input is
            returned as a zero vector rather than producing NaN.

        Raises:
            ValueError: If ``vectors`` is empty.
        """
        if not vectors:
            raise ValueError("Cannot mean-pool an empty list of vectors")

        dimensions = len(vectors[0])
        accumulator = [0.0] * dimensions
        for vector in vectors:
            norm = math.sqrt(sum(value * value for value in vector))
            if norm == 0.0:
                continue
            for index, value in enumerate(vector):
                accumulator[index] += value / norm

        pooled_norm = math.sqrt(sum(value * value for value in accumulator))
        if pooled_norm == 0.0:
            return accumulator
        return [value / pooled_norm for value in accumulator]

    def _embed_passage(self, prefix: str, value_txt: str, fact_id: object) -> list[float]:
        """
        Embed a passage, splitting and pooling it when it exceeds the context.

        Passages that fit the embedding context are sent in a single call, which
        keeps previously ingested vectors and hashes byte-identical on refresh.
        Longer ones are split into token windows of the fact value; each window
        is prefixed with the same question context so it stands alone as a
        passage, and the resulting vectors are mean-pooled into one.

        Args:
            prefix: Passage prefix ending in a separator, e.g.
                ``"Represent this fact: {question} {fact_name} "``.
            value_txt: Cleaned fact value appended to the prefix.
            fact_id: Fact identifier, used for logging only.

        Returns:
            A single embedding vector for the whole passage.
        """
        passage_text = f"{prefix}{value_txt}"
        token_limit = embedding_token_limit()
        tokens_num = count_tokens(passage_text)
        if tokens_num <= token_limit:
            return retrieve_qwen3_embedding(passage_text)

        window_size = token_limit - count_tokens(prefix) - _TOKEN_WINDOW_MARGIN
        splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            model_name=_SPLITTER_MODEL_NAME,
            chunk_size=window_size,
            chunk_overlap=0,
        )
        windows = splitter.split_text(value_txt)
        logger.info(
            "Fact %s passage is %d tokens (limit %d): embedding %d window(s) "
            "of <=%d tokens and mean-pooling",
            fact_id,
            tokens_num,
            token_limit,
            len(windows),
            window_size,
        )
        vectors = [
            retrieve_qwen3_embedding(f"{prefix}{window}") for window in windows
        ]
        return self._mean_pool(vectors)

    def ingest_for_celex_and_fact(self, celex_id: str, fact_name: str) -> int:
        """
        Embed question–fact pairs for a given celex_id and fact_name and
        upsert them into celex_facts.question2fact2embeddings.

        Algorithm:
          1. SELECT id, question, fact_name, value_md
             FROM celex_facts.question2fact
             WHERE celex_id = :celex_id AND fact_name = :fact_name
          2. For each row:
             a. Clean value_md with _clean_for_embedding().
             b. Build passage_text:
                "Represent this fact: {question} {fact_name} {value_txt}"
             c. Compute hash_q_a = MD5(passage_text).
             d. Retrieve a 4096-dim embedding via _embed_passage(): one call
                when the passage fits the embedding context, otherwise one
                call per token window, mean-pooled into a single vector.
             e. UPSERT into question2fact2embeddings:
                - INSERT (fk_fact_id, celex_id, question, fact_name,
                          value_txt, embeddings, hash_q_a, ingestion_timestamp)
                - ON CONFLICT (fk_fact_id) DO UPDATE SET
                    embeddings = EXCLUDED.embeddings,
                    hash_q_a = EXCLUDED.hash_q_a,
                    ingestion_timestamp = now()
                  (refreshes the row if the fact value has changed)
          3. Commit and return the count of rows processed (inserted or updated).

        Args:
            celex_id: Celex document identifier.
            fact_name: Target fact name to embed.

        Returns:
            Number of rows upserted (inserted or updated).

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On DB connectivity or constraint errors.
        """
        upserted = 0

        with self._session_factory() as session:
            rows = session.execute(
                text(_SELECT_FACTS_SQL),
                {"celex_id": celex_id, "fact_name": fact_name},
            ).fetchall()

            logger.debug(
                "celex_id=%s fact_name=%s: %d row(s) found in question2fact",
                celex_id,
                fact_name,
                len(rows),
            )

            for row in rows:
                fact_id, question, row_fact_name, value_md = (
                    row.id,
                    row.question,
                    row.fact_name,
                    row.value_md,
                )
                value_txt = self._clean_for_embedding(value_md or "")
                prefix = f"Represent this fact: {question} {row_fact_name} "
                passage_text = f"{prefix}{value_txt}"
                hash_q_a = self._compute_hash(passage_text)

                logger.debug("Embedding fk_fact_id=%s hash=%s", fact_id, hash_q_a)
                #!! use cache in retrieve_qwen3_embedding
                embedding: list[float] = self._embed_passage(
                    prefix, value_txt, fact_id
                )
                vector_lit = "[" + ",".join(str(x) for x in embedding) + "]"

                session.execute(
                    text(_UPSERT_SQL),
                    {
                        "fk_fact_id": str(fact_id),
                        "celex_id": celex_id,
                        "question": question,
                        "fact_name": row_fact_name,
                        "value_txt": value_txt,
                        "vec": vector_lit,
                        "hash_q_a": hash_q_a,
                    },
                )
                upserted += 1

            session.commit()

        logger.debug(
            "celex_id=%s fact_name=%s: %d row(s) upserted",
            celex_id,
            fact_name,
            upserted,
        )
        return upserted


_SELECT_ALL_FACTS_SQL = """
SELECT DISTINCT celex_id, "name"
FROM celex_facts.facts
ORDER BY celex_id, "name"
"""

_LOG_PROGRESS_EVERY = 50


class AllFactsIngestionResult(BaseModel):
    """Aggregated outcome of a full-table embedding ingestion run.

    Attributes:
        facts_processed: Number of distinct (celex_id, name) pairs submitted
            to the inner loader.
        rows_upserted: Total embedding rows inserted or refreshed across all
            fact pairs.
        errors: Per-fact error messages in the form ``"celex_id/name: <msg>"``.
            Processing continues for remaining facts when an error occurs.
    """

    facts_processed: int = Field(
        description="Distinct (celex_id, name) pairs submitted to the loader"
    )
    rows_upserted: int = Field(
        description="Total embedding rows upserted across all fact pairs"
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Per-fact error messages (celex_id/name: msg)",
    )


class AllFactsEmbeddingLoaderQwen:
    """Batch-embeds every fact in ``celex_facts.facts``.

    Iterates all distinct ``(celex_id, name)`` pairs from the facts table and
    delegates each to :class:`FactEmbeddingLoaderQwen`.  Because the inner
    loader issues an idempotent UPSERT, facts whose embeddings already exist
    are refreshed in-place without incurring a new Ollama call in the common
    case (the UPSERT resolves the conflict cheaply).

    Follows the Provisioner pattern: the SQLAlchemy session factory is injected
    at construction time so callers can substitute a test double.

    Args:
        session_factory: A callable returning a SQLAlchemy ``Session``.
            Defaults to the project-wide ``Session`` from
            ``pipelines.rag.pgvector.session``.
    """

    def __init__(
        self, session_factory: Callable[[], SessionType] | None = None
    ) -> None:
        """Initialise with an optional session factory override."""
        self._session_factory: Callable[[], SessionType] = session_factory or Session
        self._loader = FactEmbeddingLoaderQwen(session_factory=session_factory)

    def ingest_all(self) -> AllFactsIngestionResult:
        """Embed all facts and upsert their vectors.

        Algorithm:
          1. SELECT DISTINCT celex_id, "name" FROM celex_facts.facts
          2. For each unique pair call
             FactEmbeddingLoaderQwen.ingest_for_celex_and_fact(celex_id, name).
          3. Accumulate rows_upserted; capture and log errors without aborting.
          4. Return an :class:`AllFactsIngestionResult` summary.

        Returns:
            AllFactsIngestionResult with counts of processed facts, upserted
            rows, and any per-fact error messages.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the initial SELECT against
                celex_facts.facts fails.
        """
        with self._session_factory() as session:
            rows = session.execute(text(_SELECT_ALL_FACTS_SQL)).fetchall()

        pairs: list[tuple[str, str]] = [(row.celex_id, row.name) for row in rows]
        logger.info(
            "AllFactsEmbeddingLoaderQwen: %d distinct fact pair(s) found", len(pairs)
        )

        total_upserted = 0
        errors: list[str] = []

        for idx, (celex_id, fact_name) in enumerate(pairs, start=1):
            try:
                upserted = self._loader.ingest_for_celex_and_fact(celex_id, fact_name)
                total_upserted += upserted
            except Exception as exc:  # noqa: BLE001
                msg = f"{celex_id}/{fact_name}: {exc}"
                logger.warning("AllFactsEmbeddingLoaderQwen error — %s", msg)
                errors.append(msg)

            if idx % _LOG_PROGRESS_EVERY == 0:
                logger.info(
                    "AllFactsEmbeddingLoaderQwen: %d/%d pair(s) processed, "
                    "%d row(s) upserted so far, %d error(s)",
                    idx,
                    len(pairs),
                    total_upserted,
                    len(errors),
                )

        logger.info(
            "AllFactsEmbeddingLoaderQwen: finished — %d pair(s), %d row(s) upserted, %d error(s)",
            len(pairs),
            total_upserted,
            len(errors),
        )
        return AllFactsIngestionResult(
            facts_processed=len(pairs),
            rows_upserted=total_upserted,
            errors=errors,
        )


if __name__ == "__main__":
    import sys

    result = AllFactsEmbeddingLoaderQwen().ingest_all()

    print(
        f"facts_processed={result.facts_processed} "
        f"rows_upserted={result.rows_upserted} "
        f"errors={len(result.errors)}"
    )
    for err in result.errors:
        print(f"  ERROR: {err}", file=sys.stderr)

    sys.exit(1 if result.errors else 0)


#
