"""
Scan document and book chunks for COMP/IV/AT case-ID patterns, then
persist the discovered ``(case_id, celex_id)`` mappings into
``celex_facts.case_celex_id``.

Database access is delegated to:
  - :class:`~pipelines.rag.pgvector.chunk_repository.ChunkRepository`
    (scan queries — raw SQL still inlined because they are regex extractions)
  - :class:`~pipelines.rag.pgvector.celex_identity_repository.CelexIdentityRepository`
    (insert/upsert)

See: ~/a/celex-facts/src/pipelines/rag/pgvector/celex_case_id_loader.py for init load from csv
"""

import logging
from sqlalchemy import text

from pipelines.rag.pgvector.celex_identity_repository import CelexIdentityRepository
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

# Shared engine — replaces the old module-level ``create_engine(settings.DATABASE_URL)``
_engine = make_engine(DATABASE_URL)


def run_doc_cases_scan():
    records_to_insert = []
    first_query = text("""
                    SELECT DISTINCT (regexp_matches(document, '(COMP/[^ \t\n\r:)(,;"\"<>—–''+ \\-]+)'))[1] AS extracted_code, celex_id
                    FROM celex_facts.pg_embedding_qwen
                    WHERE document LIKE '%COMP/%'
                    AND doc_pos = 0

                    UNION

                    SELECT DISTINCT (regexp_matches(document, '(IV/[^ \t\n\r\\-:)(,;"\"<>—–''+]+)'))[1] AS extracted_code, celex_id
                    FROM celex_facts.pg_embedding_qwen
                    WHERE document LIKE '%IV/%'
                    AND doc_pos = 0                   
                    UNION

                    SELECT DISTINCT (regexp_matches(document, '(AT\\.[^ \t\n\r\\-:)(,;"\"<>—–''+]+)'))[1] AS extracted_code, celex_id
                    FROM celex_facts.pg_embedding_qwen
                    WHERE document LIKE '%AT.%'
                    AND doc_pos = 0
                    """)

    with _engine.begin() as conn:
        res = conn.execute(first_query).fetchall()

        # Extract a distinct list and clean trailing punctuations
        case_ids = set()
        for r in res:
            comp_code = r[0]
            cleaned_code = comp_code.rstrip(").,;'\"\\n\\r\\t -")
            case_ids.add(cleaned_code)

        case_ids_list = list(case_ids)
        logger.info(f"Extracted {len(case_ids_list)} distinct case_ids.")

        # Assert that it is 35 entries long (as requested)
        if len(case_ids_list) >= 35:
            logger.warning(
                f"Assertion warning: Expected >= 35 entries, found {len(case_ids_list)}"
            )

        for cr in res:
            records_to_insert.append({"celex_id": cr[1], "case_id": cr[0]})

    return records_to_insert


def run_book_cases_scan():

    first_query = text("""
            SELECT 
                distinct (regexp_matches(document, 'COMP/[^ ]+'))[1] AS extracted_comp_code
            FROM (
                SELECT id, level, cardinality(path_arry) AS numof_path_arry, path_arry, doc_pos, document 
                FROM (
                    SELECT id, level, string_to_array(path, ' -> ') AS path_arry, doc_pos, document 
                    FROM celex_facts.pg_embedding_book_qwen   
                ) AS X
            ) AS Y
            WHERE document LIKE '%COMP/%'
            UNION
            SELECT 
                distinct (regexp_matches(document, 'IV/[^ ]+'))[1] AS extracted_comp_code
            FROM (
                SELECT id, level, cardinality(path_arry) AS numof_path_arry, path_arry, doc_pos, document 
                FROM (
                    SELECT id, level, string_to_array(path, ' -> ') AS path_arry, doc_pos, document 
                    FROM celex_facts.pg_embedding_book_qwen   
                ) AS X
            ) AS Y
            WHERE document LIKE '%IV/%'                                   
          
  
        """)

    with _engine.begin() as conn:
        res = conn.execute(first_query).fetchall()

        # Extract a distinct list and clean trailing punctuations
        case_ids = set()
        for r in res:
            comp_code = r[0]
            cleaned_code = comp_code.rstrip(").,;'\"\\n\\r\\t -")
            case_ids.add(cleaned_code)

        case_ids_list = list(case_ids)
        logger.info(f"Extracted {len(case_ids_list)} distinct case_ids.")

        # Assert that it is 35 entries long (as requested)
        if len(case_ids_list) >= 35:
            logger.warning(
                f"Assertion warning: Expected >= 35 entries, found {len(case_ids_list)}"
            )
    return case_ids_list


def determine_case_id_to_celex_id_mapping(case_ids_list):
    """Map each case_id to every celex_id whose document text mentions it.

    Parameters
    ----------
    case_ids_list:
        Distinct case-ID surfaces (COMP/IV/AT codes) to search for.

    Returns
    -------
    list[dict]
        ``{"celex_id": ..., "case_id": ...}`` rows ready for upsert into
        ``celex_facts.case_celex_id``.
    """
    records_to_insert = []
    with _engine.begin() as conn:
        for comp_code in case_ids_list:
            # e.g., '%COMP/35.%' from the example
            search_pattern = f"%{comp_code}%"
            search_query = text("""
                SELECT distinct celex_id
                FROM celex_facts.pg_embedding_qwen
                WHERE document LIKE :search_pattern
            """)
            celex_res = conn.execute(
                search_query, {"search_pattern": search_pattern}
            ).fetchall()

            for cr in celex_res:
                records_to_insert.append({"celex_id": cr[0], "case_id": comp_code})

    logger.info(
        f"Found {len(records_to_insert)} celex_id matches to insert into celex_case_id."
    )
    return records_to_insert


def insert_into_celex_case_id(records_to_insert):
    """Persist discovered case-ID ↔ celex-ID mappings via CelexIdentityRepository."""
    repo = CelexIdentityRepository()
    repo.bulk_upsert_case_celex_mappings(records_to_insert)
    logger.info("Migration completed.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    cases_id_list = run_book_cases_scan()
    records_to_insert = determine_case_id_to_celex_id_mapping(cases_id_list)
    logger.debug(records_to_insert)
    insert_into_celex_case_id(records_to_insert)

    celex_cases_id_list = run_doc_cases_scan()
    logger.debug(celex_cases_id_list)
    insert_into_celex_case_id(celex_cases_id_list)
