import xml.etree.ElementTree as ET
import logging
from typing import Dict, Any, List

from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert

from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL
from pipelines.rag.pgvector.models import Base, CorpLei

logger = logging.getLogger(__name__)


def strip_ns(tag: str) -> str:
    """Remove XML namespace from a tag string."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def elem_to_dict(elem: ET.Element) -> Any:
    """Recursively convert an ElementTree Element into a dictionary."""
    d: Dict[str, Any] = {}
    for key, value in elem.attrib.items():
        d[f"@{strip_ns(key)}"] = value

    # Text content
    if elem.text and elem.text.strip():
        if not list(elem) and not elem.attrib:
            return elem.text.strip()
        d["#text"] = elem.text.strip()

    for child in elem:
        child_tag = strip_ns(child.tag)
        child_dict = elem_to_dict(child)
        if child_tag not in d:
            d[child_tag] = child_dict
        else:
            if not isinstance(d[child_tag], list):
                d[child_tag] = [d[child_tag]]
            d[child_tag].append(child_dict)
    return d


class CorpLEIIngestor:
    """Ingests LEI XML into PostgreSQL using Provisioner pattern."""

    def __init__(self, db_url: str = DATABASE_URL) -> None:
        self.engine = make_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)

        # Automatically create table prior to ingestion
        Base.metadata.create_all(self.engine)

    def _extract_address(self, addr_dict: Any) -> Dict[str, str]:
        if not addr_dict or not isinstance(addr_dict, dict):
            return {}

        def safe_get(key: str) -> str:
            val = addr_dict.get(key, "")
            if isinstance(val, dict) and "#text" in val:
                return str(val["#text"])
            elif val is None:
                return ""
            elif isinstance(val, list) and len(val) > 0:
                return str(val[0])
            return str(val)

        return {
            "first_line": safe_get("FirstAddressLine"),
            "additional_line": safe_get("AdditionalAddressLine"),
            "city": safe_get("City"),
            "region": safe_get("Region"),
            "postal_code": safe_get("PostalCode"),
            "country": safe_get("Country"),
        }

    def ingest_xml(self, file_path: str, batch_size: int = 3000) -> None:
        # PostgreSQL caps bind parameters at 65535.
        # CorpLei has 19 columns → max safe batch = 65535 // 19 = 3449.
        # We use 3000 to leave headroom.
        logger.info(f"Starting ingestion of {file_path}")
        context = ET.iterparse(file_path, events=("end",))

        batch: List[Dict[str, Any]] = []
        total_ingested = 0

        with self.Session() as session:
            for event, elem in context:
                tag = strip_ns(elem.tag)
                if tag == "LEIRecord":
                    record_dict = elem_to_dict(elem)

                    # Extract LEI core
                    lei_val = record_dict.get("LEI")
                    if isinstance(lei_val, dict):
                        lei_val = lei_val.get("#text", "")
                    if not lei_val:
                        elem.clear()
                        continue

                    entity = record_dict.get("Entity", {})
                    if not isinstance(entity, dict):
                        entity = {}

                    registration = record_dict.get("Registration", {})
                    if not isinstance(registration, dict):
                        registration = {}

                    legal_name = entity.get("LegalName", "")
                    if isinstance(legal_name, dict):
                        legal_name = legal_name.get("#text", "")
                    elif isinstance(legal_name, list):
                        legal_name = str(legal_name[0])

                    legal_addr_dict = entity.get("LegalAddress", {})
                    legal_address = self._extract_address(legal_addr_dict)

                    hq_addr_dict = entity.get("HeadquartersAddress", {})
                    hq_address = self._extract_address(hq_addr_dict)

                    def safe_get_str(parent: Dict[str, Any], key: str) -> str:
                        val = parent.get(key, "")
                        if isinstance(val, str):
                            return val
                        elif isinstance(val, dict):
                            return val.get("#text", "")
                        return ""

                    row = {
                        "lei": str(lei_val),
                        "legal_name": str(legal_name),
                        "entity_category": safe_get_str(entity, "EntityCategory"),
                        "legal_jurisdiction": safe_get_str(entity, "LegalJurisdiction"),
                        "entity_status": safe_get_str(entity, "EntityStatus"),
                        "registration_status": safe_get_str(
                            registration, "RegistrationStatus"
                        ),
                        "country": legal_address.get("country", ""),
                        "legal_address_first_line": legal_address.get("first_line"),
                        "legal_address_additional_line": legal_address.get(
                            "additional_line"
                        ),
                        "legal_address_city": legal_address.get("city"),
                        "legal_address_region": legal_address.get("region"),
                        "legal_address_postal_code": legal_address.get("postal_code"),
                        "headquarters_address_first_line": hq_address.get("first_line"),
                        "headquarters_address_additional_line": hq_address.get(
                            "additional_line"
                        ),
                        "headquarters_address_city": hq_address.get("city"),
                        "headquarters_address_region": hq_address.get("region"),
                        "headquarters_address_postal_code": hq_address.get(
                            "postal_code"
                        ),
                        "record_json": record_dict,
                    }

                    batch.append(row)

                    if len(batch) >= batch_size:
                        stmt = (
                            insert(CorpLei)
                            .values(batch)
                            .on_conflict_do_nothing(index_elements=["lei"])
                        )
                        session.execute(stmt)
                        session.commit()
                        total_ingested += len(batch)
                        logger.info(f"Ingested {total_ingested} records...")
                        batch = []

                    # Essential to free memory when streaming large XML files
                    elem.clear()

            # Final batch
            if batch:
                stmt = (
                    insert(CorpLei)
                    .values(batch)
                    .on_conflict_do_nothing(index_elements=["lei"])
                )
                session.execute(stmt)
                session.commit()
                total_ingested += len(batch)
                logger.info(f"Finished ingesting. Total {total_ingested} records.")


if __name__ == "__main__":
    import argparse
    import sys

    _DEFAULT_XML = (
        "~/a/celex_docs/20260421-gleif-concatenated-file-lei2.xml.69e741eccda83"
        "/20260421-gleif-concatenated-file-lei2.xml"
    )
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    parser = argparse.ArgumentParser(description="Corp LEI XML Data Ingestor")
    parser.add_argument(
        "--xml",
        required=False,
        default=_DEFAULT_XML,
        help="Path to LEI XML file (default: today's GLEIF concatenated file)",
    )
    args = parser.parse_args()

    ingestor = CorpLEIIngestor()
    ingestor.ingest_xml(args.xml)
