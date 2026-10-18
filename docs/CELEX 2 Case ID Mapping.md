
## Anomalies

40013_1084_2 --> AT_40013_5708398_1084_5 -> IV/40013

39452_3336_8 --> 39452_3384_13 --> IV/39452
-[  ] TODO: CELEX-31971D0400 --> IV/26.945  !! in document but not ingested 
37956_718_3 --> COMP/37.956 


~/a/celex-facts/src/pipelines/rag/pgvector/celex_case_id_loader.py
load csv file from former project  
csv_file = "/usr/job/a/pdf-pytesseract-ocr/docs/celex2case_id.csv"

~/a/celex-facts/src/pipelines/utils/loader_celex_id2case_id.py

```
select all cases prefixed with COMP/% with

    SELECT

        distinct (regexp_matches(document, 'COMP/[^ \t\n\r\-:)(;\"<>—–\']+'))[1] AS extracted_comp_code

    FROM (
        SELECT id, level, cardinality(path_arry) AS numof_path_arry, path_arry, doc_pos, document
        FROM (
            SELECT id, level, string_to_array(path, ' -> ') AS path_arry, doc_pos, document
            FROM celex_facts.pg_embedding_book_qwen
        ) AS X
    ) AS Y
    WHERE document LIKE '%COMP/%'
```


- [ ] query and ingest case_id in the celex_facts.pg_embedding table as well ⏫  [CELEX 2 Case ID Mapping](CELEX%202%20Case%20ID%20Mapping.md)🛫 2026-04-23 🛫 2026-04-22 02:00
