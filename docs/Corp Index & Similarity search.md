#corp_index 

author: Claude
## The Problem with Short Names + Embeddings

```
"AIG"  → embedding vector → [0.02, -0.31, ...]
"American International Group" → very different vector

"PG"   → could be Procter & Gamble, or Papua Guinea, or a film rating
```

Embedding models encode **semantic meaning from context** — but `"AIG"` has almost no context. The model may not reliably place it near `"American International Group"` unless it saw that pairing frequently in training. Qwen3 is strong, but this is a fundamental limitation.

---

## Best Strategy: Hybrid Pipeline

```sql
-- 1. Try exact / abbreviation match first (cheapest)
-- 2. Fall back to trigram for mid-length names
-- 3. Use embeddings for longer, ambiguous names

SELECT
    c1.legal_name,
    c2.company_alias,
    CASE
        -- Exact or near-exact
        WHEN lower(c1.legal_name) = lower(c2.company_alias)
            THEN 1.0
        -- Short name: use trigram only, skip embeddings
        WHEN length(c1.legal_name) <= 5 OR length(c2.company_alias) <= 5
            THEN similarity(c1.legal_name, c2.company_alias)
        -- Long names: use cosine similarity on embeddings
        ELSE 1 - (c1.name_embedding <=> c2.alias_embedding)
    END AS match_score,
    CASE
        WHEN length(c1.legal_name) <= 5 THEN 'abbreviation'
        WHEN lower(c1.legal_name) = lower(c2.company_alias) THEN 'exact'
        ELSE 'embedding'
    END AS match_method
FROM celex_facts.corp_lei c1
JOIN celex_facts.company_canonical2alias c2
    ON (
        -- Fast path: trigram for short names
        (length(c1.legal_name) <= 5 AND similarity(c1.legal_name, c2.company_alias) > 0.8)
        OR
        -- Embedding path for longer names
        (length(c1.legal_name) > 5 AND (c1.name_embedding <=> c2.alias_embedding) < 0.25)
    )
ORDER BY match_score DESC;
```

---

## For Abbreviations Specifically: Build an Expansion Table

```sql
-- One-time enrichment table
CREATE TABLE company_abbrev_expansions (
    abbrev   TEXT PRIMARY KEY,
    expanded TEXT
);

INSERT INTO company_abbrev_expansions VALUES
    ('AIG',  'American International Group'),
    ('PG',   'Procter and Gamble'),
    ('JPM',  'JPMorgan Chase'),
    ('GS',   'Goldman Sachs');

-- Then embed the EXPANDED form, not the abbreviation
SELECT
    c1.legal_name,
    COALESCE(e.expanded, c1.legal_name) AS name_for_embedding
FROM celex_facts.corp_lei c1
LEFT JOIN company_abbrev_expansions e
    ON upper(trim(c1.legal_name)) = e.abbrev;
```

You embed the expanded form → vectors are rich and comparable.

---

## When Qwen3 Embeddings Actually Shine

|Case|Trigram|Embedding|
|---|---|---|
|`"Deutsche Bank AG"` vs `"Deutsche Bank"`|✅ good|✅ good|
|`"JPMorgan"` vs `"JP Morgan Chase"`|⚠️ ok|✅ better|
|`"AIG"` vs `"American Intl. Group"`|❌ fails|❌ also fails|
|`"AIG"` vs `"AIG Inc."`|✅ good|⚠️ ok|
|Multilingual (`"Siemens AG"` vs `"Siemens"`)|✅|✅|
|Typos (`"Gooogle"` vs `"Google"`)|✅|❌ fails|

---

## TL;DR Recommendation

1. **Expand known abbreviations** before embedding (build the lookup table)
2. **Use embeddings** for names longer than ~5 chars — Qwen3 will be excellent here
3. **Keep trigram** as a fallback / secondary signal for short names and typos
4. **Composite score** = `0.6 * embedding_sim + 0.4 * trigram_sim` as a final ranking signal

The abbreviation expansion table is the highest-leverage thing you can do for your `AIG`/`PG` problem — no model will reliably solve it otherwise.


# OpenRegistry


-----------------
# Deepseek

* Get the BRIS, EUID or LEI of 
Scottish & Newcastle plc  
-->
The search results indicate that **Scottish & Newcastle Limited** is the current legal entity that was previously known as **Scottish & Newcastle plc**. This company has an LEI, though its status is listed as "lapsed".

- **Legal Entity Name**: SCOTTISH & NEWCASTLE LIMITED
    
- **LEI Code**: `5493008JEWD6T3I5ZJ82`[](https://b2bhint.com/zh/company/gb/scottish-and-newcastle-limited--SC016288)
    
- **Status**: LAPSED[](https://b2bhint.com/zh/company/gb/scottish-and-newcastle-limited--SC016288)
    
- **Previous Name**: Scottish & Newcastle PLC[](https://b2bhint.com/zh/company/gb/scottish-and-newcastle-limited--SC016288)

The European Unique Identifier (EUID) for Scottish & Newcastle Limited (Company Number SC016288) is **GBREGSC016288**. This historic UK brewing company was acquired by Heineken and Carlsberg in 2008 and largely dissolved, with its legal entity later changing to Heineken UK Limited, though the SC016288 company number is associated with its historical [Companies House records](https://find-and-update.company-information.service.gov.uk/company/SC016288).



* Get the BRIS, EUID or LEI of 
Anheuser-Busch Incorporated
-->
### Anheuser-Busch InBev SA/NV (Primary Parent Company)

This publicly traded parent company likely holds the financial and regulatory identifiers you are most interested in.

- **LEI (Legal Entity Identifier)**: The primary LEI for the company is **`5493008H3828EMEXB082`**.
    
- **BRIS / National Number**: This is a Belgian entity with a company number (**`0400.444.484`**) and legal form (**`Naamloze vennootschap / Société anonyme (NV/SA)`**).
    
- **EUID**: As a European company, its EUID would be a derivative of its national number (e.g., **EUID `BE0400.444.484`**).
    

### Anheuser-Busch Companies, LLC (U.S. Holding Company)

If you require information for the U.S.-based entity that previously operated as "Anheuser-Busch Incorporated," this is the successor company.

- **LEI**: **`5493002J2SVENP0PVK27`**[](http://openleis.com/legal_entities/5493002J2SVENP0PVK27/ANHEUSER-BUSCH-COMPANIES-LLC)
    
- **EUID**: This company would not have an EUID as it is a U.S.-based LLC.
    

### Related German Entities (with EUIDs)

Identifiable EUIDs are more commonly found for specific European subsidiaries of the group.

- **Anheuser-Busch InBev Deutschland GmbH & Co. KG** (Bremen, Germany): **`DEH1101.HRA23055HB`**
    
- **Anheuser-Busch InBev International GmbH & Co. KG** (Bremen, Germany): **`DEH1101.HRA22894HB`**



Human indexed : 'United International Pictures BV' but 
celex_facts.corp_lei
213800PC85O8BJ7DU398	UNITED GROUP B.V.

Based on the available information regarding **United International Pictures B.V.** (often abbreviated as UIP), which is a film distribution joint venture between Paramount Pictures and Universal Pictures, here are the requested identifiers:

- **LEI (Legal Entity Identifier):** **213800PC85O8BJ7DU398** (This LEI is associated with the entity listed in Amsterdam).
- **SIREN (France):** **434 425 021** (Associated with the French branch "UNITED INTERNATIONAL PICTURES B.V (UIP BVV)" listed in Paris).
- **Company Number (Netherlands):** **33127500** (Associated with Universal Studios International B.V./UIP activities in Amsterdam).
-


### Maersk Air A/S
deepseek

| Entity Name                     | Identifier Type | Identifier Value       | Status / Notes                                                                                                                                                 |
| ------------------------------- | --------------- | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A/S MAERSK AVIATION HOLDING** | CVR (Danish)    | `22765310`             | Active; Historical name included Maersk Air A/S [](http://beta.ownr.dk/companies/public-profile/22765310)[](https://datacvr.virk.dk/enhed/virksomhed/22765310) |
| **Maersk A/S**                  | LEI             | `254900BLYIXBFFRLUJ90` | Active parent company [](https://globalfinreg.com/en/lookup/Maersk-A-S/Denmark/254900BLYIXBFFRLUJ90)                                                           |
| **Maersk Air Cargo A/S**        | CVR (Danish)    | `40854126`             | Active cargo division; owns Maersk Air Cargo UK Limited                                                                                                        |
### VIBE

PROVIDE a implementation plan:

write a corp_index_module in src/pipelines/corp_index

which accept a parameter celex_id, corp_canoncical

and returns the companies BRIS, EUID or LEI form a external API call via dspy.LM liteLLM to deepseek configured in settings.config LLM_MODEL_CLOUD_ID / LLM_MODEL_CLOUD_API_KEY

sometimes the company doesnt exist anymore and the LLM returns a value such as "archived" or "lapsed" which should be returned

Return a table of this format:

corp_canoncical , BRIS, EUID, LEI, state

Example and integration test assertion

SCOTTISH & NEWCASTLE LIMITED, 'NA','NA',5493008JEWD6T3I5ZJ82, lapsed



implemented in src/pipelines/corp_index/corp_index_module.py


see: ~/a/corp_index for historic Handelsregister

select * from celex_facts.handelsregister_companies where name like 'Anheuser-Busch%'

de-oc-bremen-fruher-bremen-hra-22894	Anheuser-Busch InBev International GmbH & Co. KG	currently registered	de	Am Deich 18/19, 28199 BremenProkura: Nicht mehr Prokurist:9) Standow, Ines	2018-06-25 02:00:00.000 +0200	HRA	22894		HRA 22894		Bremen früher Bremen	
de-oc-bremen-fruher-bremen-hra-23055	Anheuser-Busch InBev Deutschland GmbH & Co. KG	currently registered	de	Am Deich 18/19, 28199 Bremen.	2018-06-25 02:00:00.000 +0200	HRA	23055		HRA 23055		Bremen früher Bremen	
de-oc-bremen-fruher-bremen-hrb-21274	Anheuser-Busch InBev Germany Holding GmbH	currently registered	de	Am Deich 18/19, 28199 Bremen.	2018-06-25 02:00:00.000 +0200	HRB	21274		HRB 21274		Bremen früher Bremen	