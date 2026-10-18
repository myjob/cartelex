import argparse
import datetime
import json
import logging
from sqlalchemy import create_engine, text
import uuid

# Reuse configuration
from pipelines.rag.pgvector.vector_config import DATABASE_URL, SEARCH_PATH

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def parse_companies(value_str: str) -> list[str]:
    """
    Splits a comma or semicolon separated list of companies into a python list.
    Safely ignores splits on commas that are part of corporate entity suffixes
    (e.g., ', Inc.', ', Ltd.', ', S.A.').
    """
    if not value_str:
        return []
    
    # If there's a semicolon, we assume it's the primary separator
    if ';' in value_str:
        return [c.strip() for c in value_str.split(';') if c.strip()]
    
    # Otherwise, split by comma but stitch back common legal entity suffixes
    raw_fragments = [c.strip() for c in value_str.split(',')]
    processed = []
    
    # Common corporate suffixes that follow a comma
    suffixes = {"inc", "inc.", "ltd", "ltd.", "llc", "llc.", "sa", "s.a.", "s.a", "plc", "plc."}
    
    for frag in raw_fragments:
        if not frag:
            continue
            
        if processed and frag.lower() in suffixes:
            # Stitch back to the previous company
            processed[-1] = f"{processed[-1]}, {frag}"
        else:
            processed.append(frag)
            
    return processed

def format_as_markdown_list(companies: list[str]) -> str:
    """Converts a python list of strings into a markdown bulleted list string."""
    if not companies:
        return ""
    return "\n".join(f"- {c}" for c in companies)

def format_as_json_array(companies: list[str]) -> str:
    """Converts a python list of strings into a JSON string array."""
    return json.dumps(companies, ensure_ascii=False)

def migrate_facts(db_url: str = DATABASE_URL):
    """
    Reads facts from rule_facts.facts, transforms the value column,
    and inserts into celex_facts.facts within a transaction.
    """
    engine = create_engine(db_url, connect_args={"options": f"-csearch_path={SEARCH_PATH}"})
    
    try:
        with engine.begin() as connection:
            logger.info("Starting migration inside transaction...")
            
            # 1. Fetch legacy facts (removed created_at)
            fetch_sql = text("""
                SELECT guid, celex_id, fact, value
                FROM rule_facts.facts
                where fact = 'human_index_party_names_all' -- !!! careful, this will overwrite existing facts !!!
                
                ;
            """)
            # and celex_id = 'CELEX-32000D0146'

            result = connection.execute(fetch_sql)
            rows = result.fetchall()
            
            logger.info(f"Fetched {len(rows)} rows from rule_facts.facts")
            
            # 2. Transform and prepare for insert
            insert_data = []
            now_iso = datetime.datetime.now().isoformat()
            for row in rows:
                guid, celex_id, fact_name, value_str = row
                
                # Parse and format the value string appropriately
                companies = parse_companies(value_str)
                value_md = format_as_markdown_list(companies)
                value_json = format_as_json_array(companies)
                # 'Anheuser-Busch International Inc; Courage Limited; Scottish & Newcastle plc; Anheuser-Busch Inc; Anheuser-Busch Companies Incorporated; Scottish Courage'
                logger.info(f"Parsed companies for CELEX ID {celex_id}: {companies}")
                insert_data.append({
                    "id": str(uuid.uuid4()),
                    "celex_id": celex_id,
                    "name": companies,
                    "value_md": value_md,
                    "value_json": value_json,
                    "created_at": now_iso, # Generated default value
                })
            
            # 3. Bulk insert into celex_facts.facts
            if insert_data:
                insert_sql = text("""
                    INSERT INTO celex_facts.facts 
                    (id, celex_id, "name", value_md, value_json, created_at)
                    VALUES (:id, :celex_id, :name, :value_md, :value_json, :created_at)
                    ON CONFLICT (id) DO NOTHING;
                """)
                result = connection.execute(insert_sql, insert_data)
                inserted = result.rowcount
                conflicts = len(insert_data) - inserted
                logger.info(f"Inserted {inserted}/{len(insert_data)} rows into celex_facts.facts ({conflicts} skipped due to id conflict)")
            else:
                logger.info("No rows to migrate.")
                


            logger.info("Transaction committed successfully.")

    except Exception as e:
        logger.error(f"Error during migration. Transaction rolled back. Details: {e}")
        raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate facts from rule_engine to celex_facts")
    parser.add_argument("--db-url", help="Override standard Vector DB URL")
    args = parser.parse_args()
    
    if args.db_url:
        migrate_facts(args.db_url)
    else:
        migrate_facts()