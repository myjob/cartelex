"""
Unified DAO for document-chunk retrieval (keyword + semantic + regex).

Consolidates the scattered chunk-level database access previously in:
  - the legacy per-celex keyword/semantic ChunkRepository (now removed)
  - ``CelexDatabaseToolkit``      (tools/celex_tools.py)             — 10+ inline SQL queries
  - ``FactRepository``            (fact_repository.py)               — select_first_page4celex

All queries go through the shared engine from ``session.py`` (single
connection pool).  The class follows the Provisioner pattern: database
client injected at ``__init__``.

The embedding concern is kept **external**: all semantic-search methods
accept a pre-computed ``query_embedding`` list instead of calling any
embedding provider.  Callers should use ``QueryEmbeddingRepository`` for
caching.

Exports:
    ChunkRepository — canonical persistence service for document chunk retrieval
"""

import logging
import re
from typing import Any, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from config.settings import settings
from extract.rag.models.chunk_vo import ChunkVo, FactSearchResult
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

# A bare SQL identifier: letters, digits and underscores only. A table name
# cannot be passed as a bind parameter, so ``embedding_table`` is interpolated
# directly into every query in this class; restricting it to this character
# class blocks the quotes, whitespace and semicolons any injection would need.
_BARE_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")


def _validate_identifier(identifier: str) -> str:
    """Return *identifier* unchanged iff it is a safe bare SQL identifier.

    Parameters
    ----------
    identifier:
        Candidate table name to be interpolated into SQL.

    Returns
    -------
    str
        The validated identifier.

    Raises
    ------
    ValueError
        If *identifier* contains anything other than ``[A-Za-z0-9_]``.
    """
    if not _BARE_IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"Unsafe SQL identifier for embedding_table: {identifier!r}")
    return identifier


class ChunkRepository:
    """Focused DAO for keyword and semantic chunk retrieval against pgvector.

    Replaces the previous ``ChunkRepository`` in ``extract/rag/`` and absorbs
    all inline SQL from ``CelexDatabaseToolkit``.

    Parameters
    ----------
    db_url:
        PostgreSQL connection string.  Defaults to ``DATABASE_URL`` from
        ``vector_config.py`` / environment.
    embedding_table:
        Unqualified table name within the ``celex_facts`` schema (e.g.
        ``"pg_embedding_qwen"``).  Defaults to ``settings.EMBEDDING_TABLE``.
        Validated as a bare SQL identifier on construction, since it is
        interpolated directly into queries.

    Raises
    ------
    ValueError
        If ``embedding_table`` is not a bare ``[A-Za-z0-9_]`` identifier.

    Invariant
    ---------
    ``self.embedding_table`` is always a validated bare identifier, so every
    ``f"... {self.embedding_table} ..."`` interpolation in this class is
    injection-safe by construction.
    """

    def __init__(
        self,
        db_url: str = DATABASE_URL,
        embedding_table: str = settings.EMBEDDING_TABLE,
    ) -> None:
        self.engine = make_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)
        self.embedding_table = _validate_identifier(embedding_table)

    # ------------------------------------------------------------------
    # Keyword search — scoped to a single CELEX document
    # ------------------------------------------------------------------

    def keyword_search(
        self, celex_id: str, keyword: str
    ) -> List[ChunkVo]:
        """Execute an ILIKE keyword search scoped to a single CELEX document.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier to scope results.
        keyword:
            The search term used for SQL ILIKE pattern matching.

        Returns
        -------
        List[ChunkVo]
            Matching chunks with ``origin="sql_keyword"`` and a fixed
            similarity of 0.5.
        """
        search_term = f"%{keyword}%"
        sql = text(f"""
            SELECT id, document, doc_pos
            FROM celex_facts.{self.embedding_table}
            WHERE document ILIKE :search_term
              AND celex_id = :celex_id
            ORDER BY doc_pos DESC
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"search_term": search_term, "celex_id": celex_id}
            ).fetchall()

        return [
            ChunkVo(
                id=str(row[0]),
                celex_id=celex_id,
                chunk=row[1],
                origin="sql_keyword",
                similarity=0.5,
                doc_pos=int(row[2]) if row[2] is not None else 0,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Keyword search — cross-case
    # ------------------------------------------------------------------

    def keyword_search_all_cases(
        self, keyword: str, hits: int = 20
    ) -> List[ChunkVo]:
        """Execute a cross-case ILIKE keyword search over all document chunks.

        Returns one representative chunk per distinct ``celex_id`` so the
        result answers "which cases mention X" entity lookups.

        Parameters
        ----------
        keyword:
            Substring matched case-insensitively against chunk text.
        hits:
            Maximum number of distinct cases to return.

        Returns
        -------
        List[ChunkVo]
            One per celex_id, with ``origin="sql_keyword"``.
        """
        search_term = f"%{keyword}%"
        sql = text(f"""
            SELECT DISTINCT ON (celex_id) id, celex_id, document, doc_pos
            FROM celex_facts.{self.embedding_table}
            WHERE document ILIKE :search_term
            ORDER BY celex_id, doc_pos
            LIMIT :hits
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"search_term": search_term, "hits": hits}
            ).fetchall()

        return [
            ChunkVo(
                id=str(row[0]),
                celex_id=row[1],
                chunk=row[2],
                origin="sql_keyword",
                similarity=0.5,
                doc_pos=int(row[3]) if row[3] is not None else 0,
            )
            for row in rows
        ]

    def fulltext_search_all_cases(
        self, query: str, hits: int = 20
    ) -> List[ChunkVo]:
        """Cross-case full-text search returning the best chunk per case.

        The multi-word counterpart to ``keyword_search_all_cases``: an ILIKE
        ``'%multi word query%'`` matches nothing, so a cross-case ``doc_search``
        for "list all cartel periods" silently fell back to semantic-only.
        The stemmed lexemes are OR-combined (see ``websearch_search_in_celex``
        for why), so multi-word questions surface the cases that mention any of
        the terms, ranked by ``ts_rank``. Returns one best-ranked chunk per
        distinct ``celex_id``.

        Parameters
        ----------
        query:
            Raw natural-language query. No keyword extraction is performed.
        hits:
            Maximum number of distinct cases to return.

        Returns
        -------
        List[ChunkVo]
            One per celex_id, ``origin="pgvector_fts"``, ``ts_rank`` in
            ``similarity``. Empty when *query* is blank.
        """
        if not query or not query.strip():
            return []

        sql = text(f"""
            WITH q AS (
                SELECT replace(
                    websearch_to_tsquery('english', :query)::text, '&', '|'
                )::tsquery AS tsq
            )
            SELECT DISTINCT ON (e.celex_id)
                   e.id, e.celex_id, e.document, e.doc_pos,
                   ts_rank(e.document_tsv, q.tsq) AS rank
            FROM celex_facts.{self.embedding_table} e, q
            WHERE e.document_tsv @@ q.tsq
            ORDER BY e.celex_id, rank DESC
            LIMIT :hits
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"query": query, "hits": hits}
            ).fetchall()

        return [
            ChunkVo(
                id=str(row[0]),
                celex_id=row[1],
                chunk=row[2],
                origin="pgvector_fts",
                similarity=float(row[4]),
                doc_pos=int(row[3]) if row[3] is not None else 0,
            )
            for row in rows
        ]

    def keyword_count_all_cases(
        self, keyword: str
    ) -> list[dict[str, Any]]:
        """Return per-case keyword hit counts across all document chunks.

        Parameters
        ----------
        keyword:
            Substring matched case-insensitively against chunk text.

        Returns
        -------
        list[dict]
            ``{"celex_id": str, "count": int}`` ordered by count descending.
        """
        search_term = f"%{keyword}%"
        sql = text(f"""
            SELECT celex_id, COUNT(*) AS cnt
            FROM celex_facts.{self.embedding_table}
            WHERE document ILIKE :search_term
            GROUP BY celex_id
            ORDER BY cnt DESC, celex_id
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"search_term": search_term}).fetchall()

        return [{"celex_id": row[0], "count": int(row[1])} for row in rows]

    # ------------------------------------------------------------------
    # Full-text search — scoped to a single CELEX document
    # ------------------------------------------------------------------

    @staticmethod
    def _section_type_clause(
        section_types: Optional[List[str]], qualifier: str = ""
    ) -> tuple[str, dict[str, Any]]:
        """Return an SQL restriction on ``section_type`` plus its bind params.

        Parameters
        ----------
        section_types:
            Allowed section types (``SectionType`` values or plain strings).
            ``None`` or empty disables filtering.
        qualifier:
            Optional table alias prefix for the column (e.g. ``"e."``).

        Returns
        -------
        tuple[str, dict[str, Any]]
            ``("", {})`` when no filtering applies, otherwise the ``AND``
            clause and the ``section_types`` bind parameter.
        """
        if not section_types:
            return "", {}
        return (
            f" AND {qualifier}section_type = ANY(:section_types)",
            {"section_types": [str(s) for s in section_types]},
        )

    def fulltext_search_in_celex(
        self,
        celex_id: str,
        query: str,
        hits: int = 120,
        section_types: Optional[List[str]] = None,
    ) -> List[ChunkVo]:
        """Execute a PostgreSQL FTS query scoped to a single CELEX document.

        Uses the stored ``document_tsv`` generated column with
        ``plainto_tsquery('english', :query)`` for safe, injection-free
        matching, ordered by ``ts_rank``. Returned ``similarity`` carries
        the raw rank score so callers can downstream-filter or compare.

        Requires the ``document_tsv`` column + GIN index from
        ``migrate_pg_embedding_qwen_add_chunk_fts.sql``.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier to scope results.
        query:
            Natural-language search string. Stop words are dropped by
            ``plainto_tsquery``; remaining lexemes are AND'd together.
        hits:
            Maximum number of matches to return.
        section_types:
            Optional ``SectionType`` values restricting matches to specific
            decision sections; ``None`` searches all sections.

        Returns
        -------
        List[ChunkVo]
            Matching chunks with ``origin="pgvector_fts"`` and the
            ``ts_rank`` score in ``similarity``.
        """
        section_clause, section_params = self._section_type_clause(section_types)
        sql = text(f"""
            SELECT id, document, doc_pos, section_type,
                   ts_rank(document_tsv, plainto_tsquery('english', :query)) AS rank
            FROM celex_facts.{self.embedding_table}
            WHERE celex_id = :celex_id
              AND document_tsv @@ plainto_tsquery('english', :query)
              {section_clause}
            ORDER BY rank DESC
            LIMIT :hits
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql,
                {"query": query, "celex_id": celex_id, "hits": hits, **section_params},
            ).fetchall()

        return [
            ChunkVo(
                id=str(row[0]),
                celex_id=celex_id,
                chunk=row[1],
                origin="pgvector_fts",
                similarity=float(row[4]),
                doc_pos=int(row[2]) if row[2] is not None else 0,
                section_type=row[3],
            )
            for row in rows
        ]

    def websearch_search_in_celex(
        self,
        celex_id: str,
        query: str,
        hits: int = 40,
        use_or: bool = True,
        section_types: Optional[List[str]] = None,
    ) -> List[ChunkVo]:
        """Full-text search a single CELEX document with ``websearch_to_tsquery``.

        The lexical leg of ``HybridSearch``. Unlike ``fulltext_search_in_celex``
        (which uses ``plainto_tsquery`` and is kept for the recall-tuned
        EnumerationSearch), this uses ``websearch_to_tsquery`` — the same
        ``document_tsv`` GIN index, but the query parser tolerates punctuation,
        quoted phrases and stray operators in a raw user question and never
        raises a syntax error. English stemming is applied on both sides, so a
        query for "breweries" matches stored "brewery"/"brewing" without any
        client-side keyword extraction or plural stemming.

        With ``use_or=True`` (the default, used by the ``HybridSearch`` recall
        leg) the stemmed lexemes are combined with **OR**, not AND. A natural
        question ANDs to nothing ("...court...ruling...judgement..." rarely all
        co-occur in one chunk), which would leave the lexical leg dead and
        defeat its purpose of catching literal surface-form mentions. OR keeps
        it recall-oriented; ``ts_rank`` still rewards chunks matching more
        terms, and RRF + the cross-encoder reranker downstream restore
        precision. The AND-to-OR flip reuses ``websearch_to_tsquery`` for all
        stemming/escaping and only rewrites the boolean operator on its output
        (injection-safe — the raw query is never concatenated into SQL).

        With ``use_or=False`` the default AND semantics is kept, so a chunk
        must contain *every* salient lexeme. This is the precision setting for
        boolean "does this document mention X" existence checks (KeywordSearch)
        where OR-ing a ubiquitous term like "case" would match everything.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier to scope results.
        query:
            Raw natural-language question. Passed straight to
            ``websearch_to_tsquery``; no keyword extraction is performed.
        hits:
            Maximum number of matches to return.
        use_or:
            OR the stemmed lexemes (recall) when True; AND them (precision)
            when False.
        section_types:
            Optional ``SectionType`` values restricting matches to specific
            decision sections; ``None`` searches all sections.

        Returns
        -------
        List[ChunkVo]
            Matching chunks with ``origin="pgvector_fts"`` and the ``ts_rank``
            score in ``similarity``. Empty when *query* is blank.
        """
        if not query or not query.strip():
            return []

        tsquery_expr = (
            "replace(websearch_to_tsquery('english', :query)::text, '&', '|')::tsquery"
            if use_or
            else "websearch_to_tsquery('english', :query)"
        )
        section_clause, section_params = self._section_type_clause(
            section_types, qualifier="e."
        )
        sql = text(f"""
            WITH q AS (SELECT {tsquery_expr} AS tsq)
            SELECT e.id, e.document, e.doc_pos, e.section_type,
                   ts_rank(e.document_tsv, q.tsq) AS rank
            FROM celex_facts.{self.embedding_table} e, q
            WHERE e.celex_id = :celex_id
              AND e.document_tsv @@ q.tsq
              {section_clause}
            ORDER BY rank DESC
            LIMIT :hits
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql,
                {"query": query, "celex_id": celex_id, "hits": hits, **section_params},
            ).fetchall()

        return [
            ChunkVo(
                id=str(row[0]),
                celex_id=celex_id,
                chunk=row[1],
                origin="pgvector_fts",
                similarity=float(row[4]),
                doc_pos=int(row[2]) if row[2] is not None else 0,
                section_type=row[3],
            )
            for row in rows
        ]

    def trigram_search(
        self, celex_id: str, term: str, hits: int = 20
    ) -> List[ChunkVo]:
        """Accent-insensitive fuzzy substring search over a CELEX document.

        Uses the accent-folded trigram GIN index (``f_unaccent(document)
        gin_trgm_ops`` from ``migrate_fts_extensions.sql``) to match a short
        entity term against chunk text tolerant of diacritics and spelling
        variants (e.g. "Maersk" against a stored ligature form). Intended for
        proper-noun / company-name lookups where the dense and FTS legs miss
        surface variants. Both sides are folded through ``f_unaccent`` so the
        planner can use the index.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier to scope results.
        term:
            Entity term to match as an accent-folded substring.
        hits:
            Maximum number of matches to return.

        Returns
        -------
        List[ChunkVo]
            Matching chunks with ``origin="pgvector_trgm"``; ``similarity``
            carries ``word_similarity`` of the term against the chunk. Empty
            when *term* is blank.
        """
        if not term or not term.strip():
            return []

        sql = text(f"""
            SELECT id, document, doc_pos, section_type,
                   celex_facts.word_similarity(
                       celex_facts.f_unaccent(:term),
                       celex_facts.f_unaccent(document)
                   ) AS sim
            FROM celex_facts.{self.embedding_table}
            WHERE celex_id = :celex_id
              AND celex_facts.f_unaccent(document)
                  ILIKE '%' || celex_facts.f_unaccent(:term) || '%'
            ORDER BY sim DESC
            LIMIT :hits
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"term": term, "celex_id": celex_id, "hits": hits}
            ).fetchall()

        return [
            ChunkVo(
                id=str(row[0]),
                celex_id=celex_id,
                chunk=row[1],
                origin="pgvector_trgm",
                similarity=float(row[4]) if row[4] is not None else 0.0,
                doc_pos=int(row[2]) if row[2] is not None else 0,
                section_type=row[3],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Semantic (vector) search — scoped to a single CELEX document
    # ------------------------------------------------------------------

    def semantic_search_in_celex(
        self,
        celex_id: str,
        query_embedding: list,
        hits: int = 40,
        section_types: Optional[List[str]] = None,
    ) -> List[ChunkVo]:
        """Execute a vector similarity search scoped to a single CELEX document.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier to scope results.
        query_embedding:
            Pre-computed embedding vector as a Python list of floats.
        hits:
            Maximum number of nearest-neighbour results to return.
        section_types:
            Optional ``SectionType`` values restricting matches to specific
            decision sections; ``None`` searches all sections.

        Returns
        -------
        List[ChunkVo]
            Matching chunks with ``origin="pgvector_semantic"`` and their
            actual cosine similarity scores.
        """
        section_clause, section_params = self._section_type_clause(section_types)
        sql = text(f"""
            SELECT id, document, doc_pos, section_type,
                   1 - (embedding <=> CAST(:qemb AS vector)) AS similarity
            FROM celex_facts.{self.embedding_table}
            WHERE celex_id = :celex_id
              AND embedding IS NOT NULL
              {section_clause}
            ORDER BY embedding <=> CAST(:qemb AS vector)
            LIMIT :hits
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql,
                {
                    "qemb": str(query_embedding),
                    "celex_id": celex_id,
                    "hits": hits,
                    **section_params,
                },
            ).fetchall()

        return [
            ChunkVo(
                id=str(row[0]),
                celex_id=celex_id,
                chunk=row[1],
                origin="pgvector_semantic",
                similarity=float(row[4]),
                doc_pos=int(row[2]) if row[2] is not None else 0,
                section_type=row[3],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Semantic search — cross-case
    # ------------------------------------------------------------------

    def semantic_search(
        self, query_embedding: list, hits: int = 40
    ) -> List[ChunkVo]:
        """Execute a vector similarity search across all CELEX documents.

        Parameters
        ----------
        query_embedding:
            Pre-computed embedding vector.
        hits:
            Maximum number of results.

        Returns
        -------
        List[ChunkVo]
            Matching chunks with ``origin="pgvector_semantic"``.
        """
        sql = text(f"""
            SELECT id, celex_id, document, doc_pos,
                   1 - (embedding <=> CAST(:qemb AS vector)) AS similarity
            FROM celex_facts.{self.embedding_table}
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:qemb AS vector)
            LIMIT :hits
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"qemb": str(query_embedding), "hits": hits}
            ).fetchall()

        return [
            ChunkVo(
                id=str(row[0]),
                celex_id=row[1],
                chunk=row[2],
                origin="pgvector_semantic",
                similarity=float(row[4]),
                doc_pos=int(row[3]) if row[3] is not None else 0,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Semantic search with full metadata (coordinates)
    # ------------------------------------------------------------------

    def semantic_search_with_meta(
        self,
        query_embedding: list,
        celex_id: Optional[str] = None,
        limit: int = 5,
        similarity_threshold: float = 0.0,
    ) -> list[dict]:
        """Semantic search returning full chunk metadata.

        Replaces ``CelexDatabaseToolkit.search_semantic_chunks`` and
        ``search_document_chunks_with_meta_coordinates``.

        Parameters
        ----------
        query_embedding:
            Pre-computed embedding vector.
        celex_id:
            Optional filter for a specific CELEX ID.
        limit:
            Maximum number of hits.
        similarity_threshold:
            Minimum similarity score (0 to 1).

        Returns
        -------
        list[dict]
            Each dict has keys ``id``, ``collection_id``, ``document``,
            ``doc_pos``, ``token_count``, ``path``, ``chunk_hash``,
            ``celex_id``, ``similarity_score``, ``distance``.
        """
        where_clause = "embedding IS NOT NULL"
        params: dict = {"qemb": str(query_embedding), "limit": limit}
        if celex_id:
            where_clause += " AND celex_id = :celex_id"
            params["celex_id"] = celex_id

        sql = text(f"""
            SELECT id, collection_id, document, doc_pos, token_count,
                   path, chunk_hash, celex_id,
                   1 - (embedding <=> CAST(:qemb AS vector)) AS similarity
            FROM celex_facts.{self.embedding_table}
            WHERE {where_clause}
            ORDER BY embedding <=> CAST(:qemb AS vector)
            LIMIT :limit
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        output = []
        for row in rows:
            sim = float(row[8])
            if sim < similarity_threshold:
                continue
            output.append({
                "id": str(row[0]),
                "collection_id": str(row[1]) if row[1] else None,
                "document": row[2],
                "doc_pos": int(row[3]) if row[3] is not None else 0,
                "token_count": row[4],
                "path": row[5],
                "chunk_hash": row[6],
                "celex_id": row[7],
                "similarity_score": sim,
                "distance": 1.0 - sim,
            })
        return output

    # ------------------------------------------------------------------
    # Semantic search over fact embeddings (question2fact2embeddings_vw)
    # ------------------------------------------------------------------

    def semantic_search_facts(
        self,
        query_embedding: list,
        hits: int = 40,
        celex_id: Optional[str] = None,
    ) -> List[FactSearchResult]:
        """Semantic search against the question-fact embedding view.

        Replaces ``CelexDatabaseToolkit.semantic_search_facts``.

        Parameters
        ----------
        query_embedding:
            Pre-computed embedding vector.
        hits:
            Maximum number of results.
        celex_id:
            Optional CELEX document identifier; omit to search all docs.

        Returns
        -------
        List[FactSearchResult]
            Ordered by descending cosine similarity.
        """
        celex_filter = "AND celex_id = :celex_id" if celex_id else ""
        sql = text(f"""
            SELECT id::text, celex_id, fact_name, question, value_txt, hash_q_a,
                   1 - (embeddings <=> CAST(:qemb AS vector)) AS similarity
            FROM celex_facts.question2fact2embeddings_vw
            WHERE embeddings IS NOT NULL
              {celex_filter}
            ORDER BY embeddings <=> CAST(:qemb AS vector)
            LIMIT :hits
        """)

        params: dict = {"qemb": str(query_embedding), "hits": hits}
        if celex_id:
            params["celex_id"] = celex_id

        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [
            FactSearchResult(
                id=row[0],
                celex_id=row[1],
                fact_name=row[2],
                question=row[3],
                value_txt=row[4],
                hash_q_a=row[5],
                similarity=float(row[6]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Distinct CELEX IDs by keyword
    # ------------------------------------------------------------------

    def select_distinct_celex_for_keyword(
        self, keyword: str
    ) -> list[dict]:
        """Return distinct celex_ids and chunk counts matching *keyword*.

        Replaces ``CelexDatabaseToolkit.select_distinct_celex_id4doc_chunk_query``.

        Parameters
        ----------
        keyword:
            Substring matched with SQL ``LIKE`` (case-sensitive).

        Returns
        -------
        list[dict]
            ``{"celex_id": str, "numof": int}`` ordered by count descending.
        """
        search_term = f"%{keyword}%"
        sql = text(f"""
            SELECT celex_id, numof FROM (
                SELECT DISTINCT celex_id, COUNT(1) numof
                FROM celex_facts.{self.embedding_table}
                WHERE document LIKE :search_term
                GROUP BY celex_id
            ) X
            ORDER BY numof DESC, celex_id
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"search_term": search_term}).fetchall()

        return [{"celex_id": row[0], "numof": row[1]} for row in rows]

    # ------------------------------------------------------------------
    # Regex retriever
    # ------------------------------------------------------------------

    def regex_retriever(
        self,
        celex_id: str,
        regex: str,
        section_types: Optional[List[str]] = None,
    ) -> list[dict[str, Any]]:
        """Extract distinct regex matches from chunks of a CELEX document.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        regex:
            PostgreSQL-style regular expression.
        section_types:
            Optional restriction to classified decision sections (e.g.
            ``["operative"]`` to read only the "HAS ADOPTED THIS DECISION"
            articles rather than every narrative mention). ``None`` scans
            the whole document.

        Returns
        -------
        list[dict]
            ``{"law_article": str}`` for each distinct match in the
            hardcoded Article whitelist.
        """
        section_clause, section_params = self._section_type_clause(section_types)
        sql = text(f"""
            SELECT DISTINCT law_article FROM (
                SELECT (regexp_matches(document, :regex, 'g'))[1] AS law_article
                FROM celex_facts.{self.embedding_table}
                WHERE celex_id = :celex_id{section_clause}
            ) x
            WHERE law_article IN (
                'Article 85', 'Article 86', 'Article 90',
                'Article 81', 'Article 82',
                'Article 101', 'Article 102', 'Article 106'
            )
            ORDER BY law_article
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"regex": regex, "celex_id": celex_id, **section_params}
            ).fetchall()

        return [{"law_article": str(row[0])} for row in rows]

    # ------------------------------------------------------------------
    # Positional retrieval (first / closing chunks)
    # ------------------------------------------------------------------

    def select_closing_chunks(self, celex_id: str, count: int) -> List[ChunkVo]:
        """Return the last *count* chunks of a document, ordered by position.

        Positional counterpart to ``select_first_page``. A decision's closing
        chunks carry the structurally fixed tail — the addressees, ``Done at
        Brussels, <date>`` and the ``For the Commission / <name> / Member of
        the Commission`` signature block — which no query vocabulary matches
        and which retrieval therefore tends to bury (see the closing reserve
        in ``HybridSearch``).

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        count:
            How many trailing chunks to return. Values below 1 return an empty
            list.

        Returns
        -------
        List[ChunkVo]
            Up to *count* chunks with ``origin="pgvector_closing"`` and
            ``similarity=0.0`` (no query was involved in selecting them),
            ordered by ascending ``doc_pos``. Empty when the CELEX has no
            chunks.
        """
        if count < 1:
            return []

        sql = text(f"""
            SELECT id, document, doc_pos, section_type
            FROM celex_facts.{self.embedding_table}
            WHERE celex_id = :celex_id
            ORDER BY doc_pos DESC
            LIMIT :count
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"celex_id": celex_id, "count": count}
            ).fetchall()

        return [
            ChunkVo(
                id=str(row[0]),
                celex_id=celex_id,
                chunk=row[1],
                origin="pgvector_closing",
                similarity=0.0,
                doc_pos=int(row[2]) if row[2] is not None else 0,
                section_type=row[3],
            )
            for row in reversed(rows)
        ]

    def select_first_page(self, celex_id: str) -> Optional[str]:
        """Return the first page (doc_pos=0) document text for a CELEX ID.

        Text-only variant. For chunk-lineage-aware retrieval (carrying the chunk
        ``id`` and ``doc_pos``) use ``FactRepository.select_first_page4celex``.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        Optional[str]
            The document text if found, otherwise ``None``.
        """
        sql = text("""
            SELECT document
            FROM celex_facts.pg_embedding_qwen
            WHERE celex_id = :celex_id AND doc_pos = 0
        """)

        with self.engine.connect() as conn:
            result = conn.execute(sql, {"celex_id": celex_id}).fetchone()
            return result[0] if result else None

    def resolve_chunk_ids_by_doc_pos(
        self, celex_id: str, doc_positions: List[int]
    ) -> List[dict[str, Any]]:
        """Resolve positional ``doc_pos`` references to stable chunk UUIDs.

        Translates the legacy ``doc_pos`` integers stored in
        ``facts.references_doc_json`` into the stable ``id`` of the matching
        ``pg_embedding_qwen`` row, so that lineage can be recorded against the
        immutable chunk identifier rather than its position.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier to scope the lookup.
        doc_positions:
            List of integer ``doc_pos`` values to resolve.

        Returns
        -------
        List[dict[str, Any]]
            One dict per matched row with keys ``chunk_id`` (UUID) and
            ``doc_pos`` (int).  Empty when ``doc_positions`` is empty or no
            rows match.
        """
        if not doc_positions:
            return []

        sql = text(f"""
            SELECT id AS chunk_id, doc_pos
            FROM celex_facts.{self.embedding_table}
            WHERE celex_id = :celex_id AND doc_pos = ANY(:positions)
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"celex_id": celex_id, "positions": doc_positions}
            ).fetchall()

        return [
            {"chunk_id": str(row.chunk_id), "doc_pos": int(row.doc_pos)}
            for row in rows
        ]

    def get_chunk_by_id(self, chunk_id: str) -> Optional[dict[str, Any]]:
        """Fetch a single chunk's raw document text by its stable id.

        Resolves a provenance ``chunk_id`` (an ``{embedding_table}.id``
        UUID-format varchar) back to the underlying chunk so the UI can
        display the exact source text behind a fact's lineage reference.

        Parameters
        ----------
        chunk_id:
            Stable id of the chunk row (``{embedding_table}.id``).

        Returns
        -------
        Optional[dict[str, Any]]
            A dict with keys ``chunk_id`` (str), ``celex_id`` (str),
            ``doc_pos`` (int | None) and ``document`` (str), or ``None`` when
            no row matches.
        """
        sql = text(f"""
            SELECT id, celex_id, doc_pos, document
            FROM celex_facts.{self.embedding_table}
            WHERE id = :chunk_id
        """)

        with self.engine.connect() as conn:
            row = conn.execute(sql, {"chunk_id": chunk_id}).fetchone()

        if row is None:
            return None
        return {
            "chunk_id": str(row.id),
            "celex_id": row.celex_id,
            "doc_pos": int(row.doc_pos) if row.doc_pos is not None else None,
            "document": row.document,
        }

    # ------------------------------------------------------------------
    # Section-classification batch support (Main_classify_sections.py)
    # ------------------------------------------------------------------

    def sum_document_tokens(self, celex_id: str) -> int:
        """Return the total stored token count across all chunks of one CELEX.

        Uses the persisted ``token_count`` column so callers can cheaply decide
        whether a whole document fits a prompt budget without tokenising any
        text. Chunks with a NULL ``token_count`` contribute zero.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier to scope the sum.

        Returns
        -------
        int
            Summed ``token_count`` for the document; ``0`` when the document has
            no chunks.
        """
        sql = text(f"""
            SELECT COALESCE(SUM(token_count), 0)
            FROM celex_facts.{self.embedding_table}
            WHERE celex_id = :celex_id
        """)

        with self.engine.connect() as conn:
            total = conn.execute(sql, {"celex_id": celex_id}).scalar()

        return int(total or 0)

    def select_chunks_for_sections(self, celex_id: str) -> List[dict[str, Any]]:
        """Fetch every chunk of one CELEX document in structural order.

        Feeds the section classifier batch: rule labels are assigned per
        chunk, forward-filled along ``doc_pos``, and already-classified rows
        act as fill anchors.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier to scope the fetch.

        Returns
        -------
        List[dict[str, Any]]
            One dict per chunk with keys ``id`` (str), ``doc_pos`` (int),
            ``document`` (str) and ``section_type`` (str | None), ordered by
            ``doc_pos``.
        """
        sql = text(f"""
            SELECT id, doc_pos, document, section_type
            FROM celex_facts.{self.embedding_table}
            WHERE celex_id = :celex_id
            ORDER BY doc_pos
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"celex_id": celex_id}).fetchall()

        return [
            {
                "id": str(row.id),
                "doc_pos": int(row.doc_pos) if row.doc_pos is not None else 0,
                "document": row.document or "",
                "section_type": row.section_type,
            }
            for row in rows
        ]

    def update_section_type(self, chunk_id: str, section_type: str) -> None:
        """Persist the classified decision section for a single chunk.

        Parameters
        ----------
        chunk_id:
            Stable id of the chunk row (``{embedding_table}.id``).
        section_type:
            A ``SectionType`` value.
        """
        sql = text(f"""
            UPDATE celex_facts.{self.embedding_table}
            SET section_type = :section_type
            WHERE id = :chunk_id
        """)

        with self.engine.begin() as conn:
            conn.execute(
                sql, {"chunk_id": chunk_id, "section_type": str(section_type)}
            )

    def select_celex_ids_missing_section_type(self) -> List[str]:
        """List CELEX ids that still have unclassified chunks.

        Drives the resumable corpus-wide classifier run: a re-run only
        revisits documents with at least one ``section_type IS NULL`` chunk.

        Returns
        -------
        List[str]
            Distinct CELEX ids, ordered, with at least one unclassified chunk.
        """
        sql = text(f"""
            SELECT DISTINCT celex_id
            FROM celex_facts.{self.embedding_table}
            WHERE section_type IS NULL AND celex_id IS NOT NULL
            ORDER BY celex_id
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql).fetchall()

        return [row.celex_id for row in rows]

    # ------------------------------------------------------------------
    # Contextual-enrichment batch support (Main_enrich_chunk_context.py)
    # ------------------------------------------------------------------

    def select_chunks_missing_context(self, celex_id: str) -> List[dict[str, Any]]:
        """Fetch the chunks of one CELEX document still lacking a context prefix.

        Feeds the contextual-retrieval enrichment batch; resumability comes
        from the ``context_prefix IS NULL`` predicate — enriched rows are
        never refetched.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier to scope the fetch.

        Returns
        -------
        List[dict[str, Any]]
            One dict per chunk with keys ``id`` (str), ``doc_pos`` (int),
            ``document`` (str) and ``section_type`` (str | None), ordered by
            ``doc_pos``.
        """
        sql = text(f"""
            SELECT id, doc_pos, document, section_type
            FROM celex_facts.{self.embedding_table}
            WHERE celex_id = :celex_id AND context_prefix IS NULL
            ORDER BY doc_pos
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"celex_id": celex_id}).fetchall()

        return [
            {
                "id": str(row.id),
                "doc_pos": int(row.doc_pos) if row.doc_pos is not None else 0,
                "document": row.document or "",
                "section_type": row.section_type,
            }
            for row in rows
        ]

    def select_celex_ids_missing_context(self) -> List[str]:
        """List CELEX ids that still have chunks without a context prefix.

        Drives the resumable enrichment run: a re-run only revisits documents
        with at least one ``context_prefix IS NULL`` chunk.

        Returns
        -------
        List[str]
            Distinct CELEX ids, ordered, with at least one unenriched chunk.
        """
        sql = text(f"""
            SELECT DISTINCT celex_id
            FROM celex_facts.{self.embedding_table}
            WHERE context_prefix IS NULL AND celex_id IS NOT NULL
            ORDER BY celex_id
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql).fetchall()

        return [row.celex_id for row in rows]

    def update_chunk_context(
        self, chunk_id: str, context_prefix: str, embedding: list[float]
    ) -> None:
        """Store a chunk's context prefix and its recomputed embedding atomically.

        One transaction so the prefix and the re-embed land together; the
        ``document_tsv`` STORED generated column recomputes automatically, so
        the FTS leg sees the prefix without further work. ``add_embeddings``
        cannot be used for this (it mints new row ids and dedups on
        ``chunk_hash``); the raw keyed UPDATE follows the established
        precedent in ``heideltime/qa/scan_dates.py``.

        Parameters
        ----------
        chunk_id:
            Stable id of the chunk row (``{embedding_table}.id``).
        context_prefix:
            The 1-2 sentence contextual-retrieval line for the chunk.
        embedding:
            The new embedding of ``context_prefix + document``.
        """
        sql = text(f"""
            UPDATE celex_facts.{self.embedding_table}
            SET context_prefix = :context_prefix,
                embedding = CAST(:embedding AS vector)
            WHERE id = :chunk_id
        """)

        with self.engine.begin() as conn:
            conn.execute(
                sql,
                {
                    "chunk_id": chunk_id,
                    "context_prefix": context_prefix,
                    "embedding": str(embedding),
                },
            )

    # ------------------------------------------------------------------
    # Language-audit batch support (Main_audit_chunk_language.py)
    # ------------------------------------------------------------------

    def select_chunks_for_language_audit(self, celex_id: str) -> List[dict[str, Any]]:
        """Fetch one CELEX document's chunk text for language detection.

        ``context_prefix`` is deliberately excluded: it is LLM-written English
        and would mask a foreign-language chunk. ``embedding`` is excluded
        because 2048 floats per row would dominate transfer for no benefit.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier to scope the fetch.

        Returns
        -------
        List[dict[str, Any]]
            One dict per chunk with keys ``id`` (str), ``doc_pos`` (int) and
            ``document`` (str), ordered by ``doc_pos``.
        """
        sql = text(f"""
            SELECT id, doc_pos, document
            FROM celex_facts.{self.embedding_table}
            WHERE celex_id = :celex_id
            ORDER BY doc_pos
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"celex_id": celex_id}).fetchall()

        return [
            {
                "id": str(row.id),
                "doc_pos": int(row.doc_pos) if row.doc_pos is not None else 0,
                "document": row.document or "",
            }
            for row in rows
        ]

    def select_celex_ids_all(self) -> List[str]:
        """List every CELEX id present in the chunk table.

        The language audit is a full-corpus read with no resume predicate: it
        writes nothing, so every run scores every document.

        Returns
        -------
        List[str]
            Distinct CELEX ids, ordered.
        """
        sql = text(f"""
            SELECT DISTINCT celex_id
            FROM celex_facts.{self.embedding_table}
            WHERE celex_id IS NOT NULL
            ORDER BY celex_id
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql).fetchall()

        return [row.celex_id for row in rows]
