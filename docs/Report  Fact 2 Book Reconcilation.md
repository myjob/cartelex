 `2026-08-04


Two tables carry `support_label`, so here are both — the cross-check ledger (which you were just querying) and the evidence verifier ledger.

**1. fact2book_checks — counts per celex_id, pivoted by label**

```sql
SELECT
    c.celex_id,
    count(*)                                                    AS facts_checked,
    count(*) FILTER (WHERE c.support_label = 'supports')           AS supports,
    count(*) FILTER (WHERE c.support_label = 'partially_supports') AS partially_supports,
    count(*) FILTER (WHERE c.support_label = 'contradicts')        AS contradicts,
    count(*) FILTER (WHERE c.support_label = 'insufficient')       AS insufficient,
    count(*) FILTER (WHERE c.support_label IS NULL)                AS no_label
FROM celex_facts.fact2book_checks c
WHERE c.action IS NOT NULL
GROUP BY c.celex_id
ORDER BY c.celex_id;
```

Long form (one row per celex_id/label) if you prefer it un-pivoted:

```sql
SELECT c.celex_id, coalesce(c.support_label, 'null') AS support_label, count(*) AS facts_numof
FROM celex_facts.fact2book_checks c
WHERE c.action IS NOT NULL
GROUP BY c.celex_id, coalesce(c.support_label, 'null')
ORDER BY c.celex_id, facts_numof DESC;
```

**2. Per-fact detail — fact name, celex_id, label** (uses `facts.celex_id`, so it also covers facts never checked):

```sql
SELECT
    f.celex_id,
    f.name          AS fact_name,
    c.support_label,
    c.action,
    c.book_chunk_id,
    c.explanation
FROM celex_facts.facts f
LEFT JOIN celex_facts.fact2book_checks c ON c.fact_id = f.id
ORDER BY f.celex_id, f.name;
```

**3. Evidence verifier ledger (`fact_evidence`)** — `celex_id` lives on `facts`, and a fact can have several chunk rows, so this rolls up to the best label per fact first:

```sql
WITH per_fact AS (
    SELECT
        f.celex_id,
        f.id AS fact_id,
        min(CASE e.support_label
                WHEN 'supports'           THEN 1
                WHEN 'partially_supports' THEN 2
                WHEN 'contradicts'        THEN 3
                WHEN 'insufficient'       THEN 4
                ELSE 5
            END) AS label_rank,
        max(e.final_score) AS best_score
    FROM celex_facts.facts f
    JOIN celex_facts.fact_evidence e ON e.fact_id = f.id
    GROUP BY f.celex_id, f.id
)
SELECT
    celex_id,
    count(*)                                    AS facts_numof,
    count(*) FILTER (WHERE label_rank = 1)      AS supports,
    count(*) FILTER (WHERE label_rank = 2)      AS partially_supports,
    count(*) FILTER (WHERE label_rank = 3)      AS contradicts,
    count(*) FILTER (WHERE label_rank = 4)      AS insufficient,
    count(*) FILTER (WHERE label_rank = 5)      AS other_label,
    round(avg(best_score), 3)                   AS avg_best_score
FROM per_fact
GROUP BY celex_id
ORDER BY celex_id;
```

Note on query 4: `fact_evidence.support_label` is free text from the verifier — besides `supports`/`partially_supports`/`contradicts`/`insufficient` it also holds provenance-class markers (`human_authored`, `derived_from_facts`, `no_provenance`, `no_candidates`, `empty_value`), which land in `other_label`. Say the word and I'll append whichever of these you want to [work-script-pgvector.sql](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/work-script-pgvector.sql).
