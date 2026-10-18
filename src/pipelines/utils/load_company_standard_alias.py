"""
Migrate company standard aliases from ``rule_facts.facts`` into
``celex_facts.company_standard2celex_id2alias``.

Database access delegates to:
  - ``CompanyRepository.insert_standard_alias`` (write)
  - ``make_engine`` (shared pool for the SELECT query on rule_facts)
"""

import json
import logging

from sqlalchemy import text

from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL
from pipelines.rag.pgvector.company_repository import CompanyRepository

logger = logging.getLogger(__name__)


def main():
    engine = make_engine(DATABASE_URL)
    company_repo = CompanyRepository(db_url=DATABASE_URL)

    select_query = text("""
        SELECT guid, celex_id, fact, question, value
        FROM rule_facts.facts
        WHERE fact = 'party_names2companies_alias'
    """)

    with engine.connect() as conn:
        rows = conn.execute(select_query).fetchall()

    insert_count = 0
    for row in rows:
        celex_id = row.celex_id
        value_str = row.value

        if not value_str:
            continue

        try:
            data = json.loads(value_str)
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON for celex_id %s: %s", celex_id, value_str)
            continue

        companies = data.get("companies", [])
        if not isinstance(companies, list):
            logger.warning(
                "Expected 'companies' to be a list for celex_id %s, got %s",
                celex_id, type(companies),
            )
            continue

        for comp in companies:
            company_name = comp.get("company", "")
            alias = comp.get("alias_corp", "")

            if company_name or alias:
                company_repo.insert_standard_alias(celex_id, company_name, alias)
                insert_count += 1

    print(f"Successfully processed {len(rows)} records and inserted {insert_count} aliases.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()