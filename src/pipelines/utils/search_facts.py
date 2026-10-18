"""
CLI for semantic search over question-fact embeddings (Qwen3).

Usage:
    python pipelines/utils/search_facts.py --query "list all fines"
    python pipelines/utils/search_facts.py --query "Export cartels" ## this results in  bolics answer
    python pipelines/utils/search_facts.py --celex_id CELEX-31975D0358 --query "list all fines"
    python pipelines/utils/search_facts.py --celex_id 62013CJ0067 --query "cartel duration" --top_k 5
"""
import argparse
import logging

from pipelines.rag.pgvector.chunk_repository import ChunkRepository
from pipelines.rag.pgvector.query_embedding_repository import QueryEmbeddingRepository
from pipelines.rag.pgvector.embeddings.qwen3_embedding import Qwen3OllamaEmbeddings

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    """Entry point for the fact semantic search CLI."""
    parser = argparse.ArgumentParser(description="Semantic search on CELEX fact embeddings.")
    parser.add_argument(
        "--celex_id",
        default=None,
        help="CELEX document identifier (optional; omit to search all documents).",
    )
    parser.add_argument("--query", required=True, help="Natural-language search query.")
    parser.add_argument(
        "--top_k", type=int, default=10, help="Maximum results to return (default: 10)."
    )
    args = parser.parse_args()

    chunk_repo = ChunkRepository()
    query_repo = QueryEmbeddingRepository()
    embeddings = Qwen3OllamaEmbeddings()

    query_embedding = query_repo.get_or_cache(args.query, embeddings.embed_query)
    results = chunk_repo.semantic_search_facts(
        query_embedding=query_embedding,
        hits=args.top_k,
        celex_id=args.celex_id,
    )

    if not results:
        print("No results found.")
        return

    scope = args.celex_id if args.celex_id else "all documents"
    print(f"\n=== FACT SEMANTIC SEARCH — {len(results)} result(s) for '{args.query}' [{scope}] ===\n")
    for i, r in enumerate(results, 1):
        print(f"[{i}] celex_id={r.celex_id} fact={r.fact_name}  similarity={r.similarity:.4f}  celex_id={r.celex_id}  ")
        print(f"     Q: {r.question}")
        print(f"     A: {r.value_txt[:200]}{'...' if len(r.value_txt) > 200 else ''}")
        print("-" * 80)


if __name__ == "__main__":
    main()


#  .venv/bin/python search_facts.py --celex_id CELEX-31975D0358 --query fines --top_k  