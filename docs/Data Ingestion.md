

## NACE Codes

Originaldokumentation: Tabelle startet auf Seite 63
[https://ec.europa.eu/eurostat/documents/3859598/5902521/KS-RA-07-015-EN.PDF](https://ec.europa.eu/eurostat/documents/3859598/5902521/KS-RA-07-015-EN.PDF)
Das hier ist das gleiche nur einfacher formatiert, das habe ich als Basis für meins genommen. Ist irgendeine Irish Handelsregister Wordpress Seite ([https://cro.ie/](https://cro.ie/)):
[https://cro.ie/wp-content/uploads/2024/04/Nace-code-2-Sept-2019.pdf](https://cro.ie/wp-content/uploads/2024/04/Nace-code-2-Sept-2019.pdf)


https://github.com/jnsprnw/nace-codes/blob/master/data/codes-2.1.json  (no class structure)
https://showvoc.op.europa.eu/#/datasets/ESTAT_Statistical_Classification_of_Economic_Activities_in_the_European_Community_Rev__2_1___NACE_2_1/unknown/downloads
stored as celex_docs/NACE_2.1.json

## case markdown 

| #   | source                 | loader                                         | table                                          | type           | dependency |
| --- | ---------------------- | ---------------------------------------------- | ---------------------------------------------- | -------------- | ---------- |
| 1   | case markdown docs     | ? loader                                       | pg_embedding_qwen                              | init-shot      |            |
|     | pg_embedding_book_qwen | src/pipelines/utils/loader_celex_id2case_id.py | celex_facts.case_celex_id ( case_id, celex_id) | on-shot loader | 1          |
|     | pg_embedding_qwen      | src/pipelines/utils/loader_celex_id2case_id.py | celex_facts.case_celex_id ( case_id, celex_id) | init-shot      | 1          |

database : celex ;  schema : celex_facts




# Fact Ingestion

## Schematic Architecture Overview
![Pasted image 20260425124616](assets/Data%20Ingestion/Pasted%20image%2020260425124616.png)

pipelines
case_title: case id and title from the first document page if available. case_id is also extracted from book and occurance within document and ingested in table case_celex_id




## events & dates with heideltime
based on [[https://github.com/HeidelTime/heideltime]]

install [py_heideltime](https://github.com/hmosousa/py_heideltime) and a JRE (sudo apt install default-jre or sudo apt install openjdk-22-jre-headless) 
CPU heavy TimeML java process 


### fact heideltime_event_agg

| EVENT      | ACTOR      | PREDICATE OBJECT                       |
|:-----------|:-----------|:---------------------------------------|
| 1999-12-14 | Commission | issued Decision on fines and liability |
| 1998-08-04 | Commission | sent Statement of Objections           |

## timetable (obsolete)
the module responsible for ingesting the "timetable" (which is stored under the fact name `"timeline"`) into the fact table is **`TimelineFactIngestor`**. using AGNO
- [ ] migrate from agno to dspy and add to FACT_ingestion pipelines
It is located in the following module: 📄 **`src/pipelines/rag/pgvector/timeline_table.py`**
To run this ingestion process in batch mode across your dataset, the system uses the following batch script: 📄 **`src/batch/Main_ingest_timeline_table.py`**



# company, company canonical, and corp indices

human curated list: ./src/pipelines/data/company_canonical_alias_human.csv -> company_standard2celex_id

see challenges with company similarity search here  [Corp Index & Similarity search](docs/Corp%20Index%20&%20Similarity%20search.md)

lei2company_canonicalmatches_mv and  lei2company_canonical2alias_matches_mv ma LEI legal names with company in a materialized view.
![Pasted image 20260429092217](assets/Data%20Ingestion/Pasted%20image%2020260429092217.png)


## Deutsches Handelsregister
https://www.opensanctions.org/datasets/de_offeneregister/
~/a/corp_index/entities.ftm.json