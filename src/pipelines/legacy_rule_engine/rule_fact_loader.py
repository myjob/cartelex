import argparse
import json
import logging
from pathlib import Path
from sqlalchemy import text

from pipelines.rag.pgvector.session import make_engine

logger = logging.getLogger(__name__)

def parse_rule_facts(json_data):
    """
    Parses rule facts from JSON data according to the schema:
    guid (DB generated), celex_id, fact, question, value
    """
    kb = json_data.get("knowledge_base", {})
    celex_id = kb.get("CELEX")
    if not celex_id:
        logger.warning("No CELEX ID found in knowledge_base.")
        return []

    facts = kb.get("facts", {})
    records = []
    
    for key, value in facts.items():
        if isinstance(value, dict):
            # Extract from dictionary structure
            fact_tag = value.get("tag", key)
            question = value.get("prompt_question", "NA")
            
            # Handle 'value' vs 'values'
            val = value.get("value")
            if val is None:
                val = value.get("values", "")
        else:
            # Extract from simple key-value structure
            fact_tag = key
            question = "NA"
            val = str(value)
            
        records.append({
            "celex_id": celex_id,
            "fact": str(fact_tag),
            "question": str(question),
            "value": str(val)
        })
        
    return records

def load_data(records: list[dict[str, str]]) -> None:
    """Load parsed records into the ``rule_facts.facts`` table.

    The connection string comes from ``settings.DATABASE_URL`` via
    ``make_engine``; the target table is schema-qualified, so the engine's
    search_path is irrelevant here.

    Args:
        records: Rows as produced by :func:`parse_rule_facts`.

    Returns:
        None. Loading is skipped when facts already exist for the CELEX ID.
    """
    if not records:
        logger.warning("No records to load.")
        return

    engine = make_engine()
    
    # Check if facts for this CELEX ID already exist
    celex_id = records[0].get("celex_id")
    if celex_id:
        try:
            with engine.connect() as connection:
                result = connection.execute(
                    text("SELECT COUNT(*) FROM rule_facts.facts WHERE celex_id = :celex_id"), 
                    {"celex_id": celex_id}
                ).scalar()
                if result > 0:
                    logger.info(f"Skipping load: {result} facts already exist for CELEX ID {celex_id}.")
                    return
        except Exception as e:
            logger.error(f"Error checking existing facts: {e}")
            return
            
    # The records are already a list of uniform dicts, which is exactly what
    # executemany wants -- the DataFrame that used to sit here existed only to
    # reach to_sql().
    insert_stmt = text(
        "INSERT INTO rule_facts.facts (celex_id, fact, question, value) "
        "VALUES (:celex_id, :fact, :question, :value)"
    )

    try:
        with engine.begin() as connection:
            connection.execute(insert_stmt, records)
        logger.info(f"Successfully loaded {len(records)} facts into rule_facts.facts")
    except Exception as e:
        logger.error(f"Error loading data to Database: {e}")

def process_file(file_path: Path | str) -> None:
    """Parse one JSON knowledge-base file and load its facts.

    Args:
        file_path: Path to the JSON file.

    Returns:
        None. Unreadable files are logged and skipped.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Error reading JSON from {file_path}: {e}")
        return
        
    records = parse_rule_facts(data)
    if records:
        load_data(records)

def process_directory(dir_path: Path | str) -> None:
    """Parse and load every ``*.json`` file found under a directory tree.

    Args:
        dir_path: Directory to walk recursively.

    Returns:
        None. A non-directory path is logged and skipped.
    """
    path = Path(dir_path)
    if not path.is_dir():
        logger.error(f"{dir_path} is not a valid directory.")
        return
        
    json_files = list(path.glob("**/*.json"))
    logger.info(f"Found {len(json_files)} JSON files to process in {dir_path}")
    
    for json_file in json_files:
        logger.info(f"Processing {json_file}...")
        process_file(json_file)

def main() -> None:
    """CLI entry point: load rule facts from a JSON file or directory.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Load JSON facts into rule_facts.facts table.")
    parser.add_argument("input_path", help="Path to a JSON file or directory containing JSON files.")

    args = parser.parse_args()
    
    path = Path(args.input_path)
    if path.is_file():
        process_file(path)
    elif path.is_dir():
        process_directory(path)
    else:
        logger.error(f"Input path {args.input_path} does not exist.")

if __name__ == "__main__":
    main()
