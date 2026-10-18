#corp 
There is no single "Excel-style" download for every company in Europe, but there are several official and high-quality registers you can use. Since you need a **primary key** for 5,000 companies, the choice depends on whether your list is strictly EU-based or includes global entities, and whether they are small private firms or large public ones.

Here are the best options for a primary key in 2026:

### 1. The European Unique Identifier (EUID)

This is the official identifier for the **Business Registers Interconnection System (BRIS)**. It was designed specifically to link all national registers (like France's SIREN or Germany's Handelsregister) into one European system.

- **Format:** `[Country Code] [Register ID] . [Registration Number]`
    
- **Best for:** A strictly European list. It covers **every** registered company (private or public) in the EU, Iceland, Liechtenstein, and Norway.
    
- **How to get it:** You can search for companies via the [European e-Justice Portal](https://www.google.com/search?q=https://e-justice.europa.eu/content_business_registers_at_european_level-105-en.do).
    
- **Bulk Access:** Official bulk access is often restricted to "Information Distributors" (formerly via the EBR, which closed in March 2026). For a list of 5,000, you may need to use a third-party aggregator like **OpenCorporates**.
    

### 2. The Legal Entity Identifier (LEI) — _Recommended_

If your 5,000 companies are medium-to-large or publicly traded, the **LEI** is the superior primary key. It is a 20-digit alphanumeric code governed by the Global Legal Entity Identifier Foundation (GLEIF).

- **Format:** `54930084UKLVMY22DS16` (20 characters).
    
- **Best for:** Data integrity. It is a **global standard**, and as of 2026, it is mandatory for almost all European companies involved in financial markets or regulated by the **Digital Operational Resilience Act (DORA)**.
    
- **Bulk Access:** This is the easiest to get. You can download the entire global database (millions of records) for free in CSV or XML format from the [GLEIF Golden Copy](https://www.gleif.org/en/lei-data/gleif-golden-copy) files. This makes it a perfect primary key for data scientists.

#### Schema
https://www.gleif.org/en/lei-data/access-and-use-lei-data/level-1-data-lei-cdf-3-1-format
https://www.gleif.org/lei-data/access-and-use-lei-data/level-1-data-lei-cdf-3-1-format/2021-03-04_lei-cdf-v3-1.xsd

####  implemention
feat(corp-lei): add GLEIF LEI XML ingestion pipeline

Add a memory-efficient streaming ingestor for the GLEIF concatenated
LEI Common Data File (CDF v3.1) into celex_facts.corp_lei.

Models (models.py)
- Add CorpLei ORM model (schema: celex_facts, table: corp_lei)
- Explicit columns for legal address and HQ address fields
- record_json JSONB column stores the full parsed LEI record

Ingestor (corp_lei_index_ingestor.py)
- CorpLEIIngestor follows the Provisioner pattern (engine injected via __init__)
- Streams 3.7 GB XML with iterparse + elem.clear() to keep memory flat
- Bulk-inserts in configurable batches (default 5 000) via SQLAlchemy Core
  INSERT … ON CONFLICT DO NOTHING for idempotent re-runs
- Table auto-created via Base.metadata.create_all on first run
- CLI entry point: python corp_lei_index_ingestor.py --xml <path>

Schema (corp_lei_cdf.xsd)
- Add GLEIF LEI-CDF 3.1 XSD for reference

Tests (tests/test_corp_lei_ingestor.py)
- TestCorpLeiIngestorUnit  – 8 fully mocked unit tests covering
  strip_ns, elem_to_dict, _extract_address and ingest_xml batch logic
- TestCorpLeiIngestorIntegration – 2 live round-trip tests against the
  celex database; tearDown cleans up the sentinel row for idempotency
- All 10 tests pass (0.2 s unit, <1 s integration)

default input XML file
*  ~/a/celex_docs/20260421-gleif-concatenated-file-lei2.xml.69e741eccda83/20260421-gleif-concatenated-file-lei2.xml

### 3. OpenCorporates ID

OpenCorporates is the largest open database of companies in the world. They have created their own "primary key" system which is very popular in the tech industry.

- **Format:** `[Jurisdiction Code] / [Company Number]` (e.g., `gb/01234567`).
    
- **Best for:** Ease of use and web-scraping. Their API is robust and allows you to match names to IDs efficiently.
    
- **Note:** This is a "private" identifier, but it is widely treated as a de-facto standard in open data.
    

---

### Comparison Table for Your List

|**Feature**|**EUID (Official EU)**|**LEI (Global Standard)**|**OpenCorporates ID**|
|---|---|---|---|
|**Scope**|EU + EEA only|Global|Global|
|**Coverage**|100% of registered firms|Public & regulated firms|~100% of visible registers|
|**Bulk Download**|Difficult (Paid/API)|**Very Easy (Free CSV)**|API / Subscription|
|**Stability**|High|Very High|High|
|**Suitability**|Best for legal/official use|Best for data analysis|Best for developers|

### Current Trend: The "EU Inc." Register (New for 2026)

In **March 2026**, the European Commission launched the **"EU Inc."** initiative. This is a new, optional corporate form that allows companies to register directly at the EU level rather than via national systems. If your list includes very new startups (founded after March 2026), they might have a direct **EU-level registration number**, which will eventually be the ultimate primary key for the single market.

**My Advice:** If your list consists of established companies, use the **LEI** as your primary key. It is the cleanest, most "database-ready" identifier available for free.

Do you already have the names of these 5,000 companies, or are you looking for a source to generate the list from scratch?