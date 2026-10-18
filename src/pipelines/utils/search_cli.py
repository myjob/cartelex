"""
CLI for searching CELEX document chunks via keyword, semantic, or hybrid search.

The --query value is interpreted according to --type:
  keyword  → used as the SQL ILIKE keyword
  semantic → used as the semantic/vector query
  hybrid   → used as both keyword and semantic query

Usage:
    python src/pipelines/utils/search.py --type semantic --query "Export cartels"
    python src/pipelines/utils/search.py --type hybrid  --celex_id 62013CJ0067 --query "fine amount"
    python src/pipelines/utils/search.py --type semantic --celex_id 62013CJ0067 --query "penalty for cartel"
    python src/pipelines/utils/search.py --type keyword  --celex_id 62013CJ0067 --query fine
    python src/pipelines/utils/search.py --type hybrid  --celex_id 62013CJ0067 --query "fine amount" --top_k 5
"""
import argparse
import logging

from extract.rag.hybrid_search import HybridSearch
from extract.rag.key_word_search import KeywordSearch
from extract.rag.ranker.ranker_factory import create_ranker
from extract.rag.models.chunk_vo import RankedChunkVo
from config.settings import settings
from pipelines.rag.pgvector.chunk_repository import ChunkRepository
from pipelines.rag.pgvector.query_embedding_repository import QueryEmbeddingRepository
from pipelines.rag.pgvector.embeddings.qwen3_embedding import Qwen3OllamaEmbeddings

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def print_results(results: list[RankedChunkVo], search_type: str) -> None:
    if not results:
        print("No results found.")
        return

    print(f"\n=== {search_type.upper()} SEARCH — {len(results)} result(s) ===\n")
    for i, chunk in enumerate(results, 1):
        score_info = ""
        if isinstance(chunk, RankedChunkVo):
            score_info = f"  score={chunk.score:.4f}  base={chunk.base_score:.4f}"
        print(f"[{i}] id={chunk.id}  origin={chunk.origin}  doc_pos={chunk.doc_pos}{score_info}")
        print(chunk.chunk)
        print("-" * 80)


def main() -> None:
    parser = argparse.ArgumentParser(description="Search CELEX document chunks.")
    parser.add_argument(
        "--type",
        dest="search_type",
        choices=["keyword", "semantic", "hybrid"],
        default="hybrid",
        help="Search strategy to use (default: hybrid).",
    )
    parser.add_argument("--celex_id", default=None, help="CELEX document identifier.")
    parser.add_argument("--query", required=True, help="Query string; interpreted as keyword, semantic query, or both depending on --type.")
    parser.add_argument("--top_k", type=int, default=None, help="Limit returned results.")
    args = parser.parse_args()

    # Lightweight repository setup — no CelexDatabaseToolkit overhead.
    # The embedding concern is kept external (Provisioner pattern): the repo
    # takes pre-computed vectors, and ``embed_fn`` is passed to the search
    # orchestrators that need semantic retrieval.
    embeddings = Qwen3OllamaEmbeddings()
    query_repo = QueryEmbeddingRepository()

    def embed_fn(q: str) -> list:
        return query_repo.get_or_cache(q, embeddings.embed_query)

    chunk_repo = ChunkRepository()

    if args.celex_id is None:
        results = chunk_repo.select_distinct_celex_for_keyword(args.query)
        if not results:
            print("No matching CELEX documents found.")
            return
        print(f"\n{'CELEX ID':<30} {'chunks':>6}")
        print("-" * 38)
        for row in results:
            print(f"{row['celex_id']:<30} {row['numof']:>6}")
        return

    if args.search_type == "keyword":
        searcher = KeywordSearch(repo=chunk_repo)
        results = searcher.search(
            celex_id=args.celex_id,
            query=args.query,
            top_k=args.top_k,
        )

    elif args.search_type == "semantic":
        chunks = chunk_repo.semantic_search_in_celex(
            celex_id=args.celex_id,
            query_embedding=embed_fn(args.query),
            hits=args.top_k or settings.SEMANTIC_SEARCH_HITS,
        )
        ranker = create_ranker()
        results = ranker.rank_chunkvo(args.query, chunks, top_k=args.top_k)

    else:  # hybrid
        ranker = create_ranker()
        searcher = HybridSearch(repo=chunk_repo, ranker=ranker, embed_query=embed_fn)
        results = searcher.search(
            celex_id=args.celex_id,
            query=args.query,
            top_k=args.top_k,
        )

    print_results(results, args.search_type)


if __name__ == "__main__":
   main()
   
