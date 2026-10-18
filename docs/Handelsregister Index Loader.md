#corp_index #corp

The file is at [handelsregister_loader.py](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/handelsregister_loader.py).
- [ ] migrate vscode links to Wiki links for github

**What it does:**

- **Single streaming pass** over the 13M-line FTM file, handling four entity schemas:
    - `Company` / `Organization` → `handelsregister_companies` + `handelsregister_previous_names` (batched with `ON CONFLICT DO NOTHING`)
    - `Person` → `hr_persons_tmp` session-scoped temp table
    - `Directorship` → `hr_directorships_tmp` session-scoped temp table
- **SQL JOIN** at the end to populate `handelsregister_officers` from the two temp tables — avoids loading 3.5M persons into Python memory
- `TRUNCATE handelsregister_companies CASCADE` at the start (optional, `--no-truncate` to skip)
- `register_art` / `register_nummer` / `registrar` parsed from the `registrationNumber` list (short form "HRB 16509" vs long form "Aachen 73 HRB 16509")
- `handelsregister_company_additional_data` is intentionally left empty — the FTM format has no boolean publication flags (AD, CD, DK…)

**Run it:**


.venv/bin/python -m pipelines.rag.pgvector.handelsregister_loader --ftm ~/a/corp_index/entities.ftm.json


Load succeeded cleanly:

- **5,461,351** companies ingested
- **1,345,716** previous names
- **3,548,975** persons staged
- **3,496,627** directorships → **3,496,627** officer rows inserted via the SQL JOIN

Total runtime ~24 minutes (streaming ~20 min, officer JOIN ~14 min). The 14-minute JOIN across unindexed temp tables on 3.5M rows is expected — adding indexes on `hr_directorships_tmp(organization_id)` and `hr_persons_tmp(id)` before the JOIN would cut that significantly if re-runs become frequent.