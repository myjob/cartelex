I have build a pipeline processing hundreds legal documents and extracting >50 facts. sometimes I experience data quality errors caused by former extraction tasks and have to redo certain tasks for a subset of the documents. What are patterns and industry practices for this re-iteratative run of data pipelines based on quality checks?

' >>> 

What you’re describing is a **very common problem in complex, multi‑stage data pipelines**, especially for NLP / document‑intelligence workflows with legal content. Mature data teams don’t “re‑run everything”; they design for **selective, quality‑driven reprocessing**.

Below are **proven industry patterns and practices**, moving from foundational principles to concrete architectural patterns you can apply.

---

**1. Treat the Pipeline as _Deterministic + Replayable_**

**Core principle**  
Every transformation step should be:

- **Deterministic** (same input → same output)
- **Replayable** without side effects

**Practices**

- Immutable raw inputs (original documents are never overwritten)
- Outputs are _derived artifacts_ versioned by:

- document_id
- pipeline_step
- step_version (or model_version)

- No in‑place updates; always **append + supersede**

This is the basis that makes selective re‑execution _possible at all_.

---

**2. Fine‑Grained Lineage & Dependency Tracking (Document × Step)**

Instead of thinking “pipeline run”, think:

**(document_id, extraction_task, version) → result**

**Industry patterns**

- **Task‑level lineage**, not job‑level lineage
- Explicit dependency graph: OCR → Layout → Clause Detection → Fact Extraction → Normalization

**What this enables**

- Re‑run _only_:

- affected documents
- downstream steps

- Skip upstream steps if inputs unchanged and valid

**Practical implementations**

- Store metadata per task result:

- input_hash
- upstream_task_ids
- model/prompt/version
- timestamps

- Tools used in practice:

- Airflow + metadata DB
- Dagster / Prefect (native asset lineage)
- Custom tables (works surprisingly well)

---

**3. Data Quality Checks as First‑Class Pipeline Outputs**

A key industry insight:  
**DQ checks are not “assertions”, they are data products.**

**Pattern: _Validate → Annotate → Decide_**

Each task produces:

- result
- quality_signals
- quality_status

Example:

{

  "fact": "governing_law",

  "value": "England and Wales",

  "confidence": 0.62,

  "quality_checks": {

    "confidence_min": false,

    "regex_match": true,

    "cross_field_consistency": true

  },

  "quality_status": "FAILED"

}

**Result**

- Quality failures are **queryable**
- You can select exactly _which documents_ and _which steps_ need re‑work

---

**4. Selective Re‑processing Patterns**

**4.1 “Invalidate & Recompute” (Most Common)**

**How it works**

- When a quality issue is detected:

- Mark specific task outputs as invalid
- Downstream tasks automatically become stale

- Scheduler re‑runs only invalid/stale nodes

**Widely used in**

- Data warehouses
- Feature stores
- ML pipelines

**Equivalent mental model**

make or dbt for document pipelines

---

**4.2 Backfill by Predicate (Targeted Reruns)**

Instead of re‑running “run #123”:

SELECT document_id

FROM facts

WHERE

  extraction_version = 'v2'

  AND confidence < 0.7

  AND document_type = 'MSA'

``

Then:

- Re-run **only Fact Extraction v3**
- Only for those documents
- Skip OCR / layout / clause detection

This is _extremely_ common in legal & compliance data platforms.

---

**4.3 Branch‑and‑Merge (When You Change Logic)**

Used when you:

- Fix a prompt
- Update an extraction rule
- Improve normalization logic

**Pattern**

- Run new version in parallel
- Compare:

- coverage
- accuracy
- regressions

- Promote results and mark old ones deprecated

This mirrors **ML model shadow deployment**.

---

**5. Version Everything (Models, Prompts, Rules)**

For NLP pipelines this is critical.

**Best practice**

Every extraction result must be traceable to:

- model family (e.g., GPT‑4.1, custom LLM)
- prompt/template version
- post‑processing rule version

**Why**

- You can _surgically_ re‑run:

“All documents processed with prompt v12 that failed confidence thresholds”

Without this, re‑processing becomes guesswork.

---

**6. Partial Re‑execution via Checkpointing**

This avoids recomputation cost.

**Pattern**

Each step:

- declares its **inputs**
- stores a fingerprint (hash)

If:

- inputs unchanged
- step logic unchanged → **reuse cached output**

This is standard in:

- Spark (checkpointing)
- ML feature stores
- DAG‑based orchestrators

---

**7. Human‑in‑the‑Loop as a Controlled Override**

In legal pipelines, 100% automation is unrealistic.

**Industry practice**

- Failed quality cases flow into **review queues**
- Reviewer can:

- correct a fact
- approve a low‑confidence extraction

- Corrections are stored as:

- authoritative overrides
- training / prompt‑tuning feedback

Crucially:

- Manual fixes do **not** trigger full re‑runs
- They are layered _above_ automated outputs

---

**8. Operational Metrics That Trigger Re‑runs**

Mature systems don’t react manually.

**Common triggers**

- Confidence distribution shifts
- Spike in null / missing fields
- Cross‑document inconsistency (same counterparty, different facts)
- Schema coverage regression after a deployment

These automatically queue _selective_ reprocessing jobs.

---

**9. Reference Architecture (Concrete)**

**Typical enterprise setup**

- Object storage: immutable raw + derived artifacts
- Metadata DB:

- document_id
- task
- version
- quality_status

- Orchestrator:

- DAG with conditional execution

- DQ framework:

- rules as code
- results persisted, not just logged

---

**10. Anti‑Patterns to Avoid**

![❌](https://fonts.gstatic.com/s/e/notoemoji/17.0/274c/72.png) Re‑running the entire pipeline  
![❌](https://fonts.gstatic.com/s/e/notoemoji/17.0/274c/72.png) Overwriting results  
![❌](https://fonts.gstatic.com/s/e/notoemoji/17.0/274c/72.png) No versioning of prompts/models  
![❌](https://fonts.gstatic.com/s/e/notoemoji/17.0/274c/72.png) Quality checks that only log (not persisted)  
![❌](https://fonts.gstatic.com/s/e/notoemoji/17.0/274c/72.png) Pipeline runs without document‑level selectors

---

**If You Want a One‑Sentence Summary**

**Industry best practice is to design document pipelines as versioned, replayable DAGs with task‑level lineage and persisted quality signals, enabling selective invalidation and targeted reprocessing instead of full reruns.**

If you want, I can:

- Map this to **Airflow / Dagster / Prefect**
- Give an **example schema** for task + quality tracking
- Show how this looks specifically for **LLM‑based extraction pipelines**