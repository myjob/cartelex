#!/usr/bin/env bash
cd ~/a/celex-facts

usage() {
    echo "Usage: $0 [celex_id] [query] [top_k]"
    echo "  celex_id  : CELEX document identifier (optional; omit to search all documents)"
    echo "  query     : natural-language search query (optional; prompted in loop)"
    echo "  top_k     : max results to return (default: 10)"
    echo "  Enter 'q' at the query prompt to quit."
}

CELEX_ID="${1:-}"
TOP_K="${3:-}"

# If a query was supplied on the command line, run it once then enter the loop.
if [ -n "$2" ]; then
    args=(--query "$2")
    [ -n "$CELEX_ID" ] && args+=(--celex_id "$CELEX_ID")
    [ -n "$TOP_K" ] && args+=(--top_k "$TOP_K")
    ./.venv/bin/python src/pipelines/utils/search_facts.py "${args[@]}"
fi

while true; do
    echo ""
    read -rp "Query (q to quit): " QUERY
    case "$QUERY" in
        q|Q) echo "Bye."; break ;;
        "")  usage ;;
        *)
            args=(--query "$QUERY")
            [ -n "$CELEX_ID" ] && args+=(--celex_id "$CELEX_ID")
            [ -n "$TOP_K" ] && args+=(--top_k "$TOP_K")
            ./.venv/bin/python src/pipelines/utils/search_facts.py "${args[@]}"
            ;;
    esac
done
