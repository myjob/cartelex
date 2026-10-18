# Keywords
in fact_extractor.yaml the pipeline field is a keyword that maps to an agent.
The agents are available in the batch job runner:

| pipeline | agent | flow |
| --- | --- | --- |
| `universal` | `UniversalFactModule` | sql existence -> hybrid search -> llm extraction -> llm verification -> chunk reference -> fact ingestion |
| `doc_search`| `DocSearchModule` | sql existence -> hybrid keyword search -> chunk reference -> fact ingestion |
| `keyword_search`| `DocKeywordSearchModule` | sql existence -> hybrid keyword search -> chunk reference -> fact ingestion |
| `fact_context`| `FactContextModule` | sql existence -> context gathering (from predecessors) -> llm extraction -> fact ingestion |
| `fact_comparison`| `FactComparisonModule` | fetch legacy & new facts -> llm comparison -> fact ingestion |
| `qa_retrieval`| `QARetrievalPipeline` | planner (question decomposition) -> executor (kb_sql / case_facts / rag_search / rag_search_in_case) -> synthesizer (grounded answer + citations) |

all modules are first checking for the existence of the fact in the database.
if the fact exists, it will be used to populate the fact_extractor.yaml file.
if the fact does not exist, it will be extracted from the document or the already existing facts.
the parameter `force_override: true` will force the module to extract the fact even if it exists in the database.