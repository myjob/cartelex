#ingestion
# Design — Markdown Translator

`batch.Main_translate_markdown` turns a non-English decision Markdown (German, French, Italian, Dutch, ...) into an English Markdown and makes the English file the decision's working ("master") document. The original stays on disk and stays referenced. Fact extraction and RAG ingestion then read English only.

Spec: [2026-10-31-markdown-translate-design](claude/plans/2026-10-31-markdown-translate-design.md). Plan: [2026-10-31-markdown-translate-implementation](claude/plans/2026-10-31-markdown-translate-implementation.md). Upstream: [Design Downloader](Design%20Downloader.md) (run book step 7), [MinerU conversion design](claude/plans/2026-10-31-mineru-pdf-markdown-conversion-design.md).

---

## 1. Intent and non-goals

- One English Markdown per decision, written next to its source: `<input dir>/<input stem>_EN.md`.
- The registry **source row** is switched to the English file; the foreign Markdown is kept in `original_file_md`. There is no separate registry row per translation.
- A decision is translated completely or not at all. A failed decision is rolled back and skipped; it never stops the run.
- Not in scope: re-chunking or re-embedding (the RAG ingest batch runs separately), translating anything other than the source row's Markdown, choosing between language candidates.

## 2. Data model

| Where | Field | After translation |
|---|---|---|
| `document_registry` (source row) | `local_file_md` | English `<stem>_EN.md` |
| | `original_file_md` | foreign Markdown the English one came from (kept on re-translation) |
| | `language` | original language, upper case (`DE`); filled by detection when it was NULL/NA |
| `decision_registry` | `language_translated` | `en` -- **the lazy-mode "done" marker** |
| master JSON entry | `txt_md_file` | English `<stem>_EN.md` |
| | `original_file_md` | foreign Markdown |
| | `language_init` / `language_translated` | `de` / `en` (lower case) |
| `document_translation_audit` | one row per queue item per run | see section 6 |

The lazy marker is the decision flag, not the file on disk: a `_EN.md` left behind by a crash between file swap and commit does not make a decision look done; the next run overwrites it.

## 3. Flow

```
queue (source rows, local_file_md set, language <> EN; lazy: language_translated <> 'en')
  -> input = original_file_md or local_file_md
  -> no original and the input detects as English -> already_english (force never translates English to English)
  -> language: stored, else detect_markdown_language (EN -> already_english, stored)
  -> TokenTargetMarkdownSplitter (TRANSLATE_CHUNK_TOKENS, sentence-aware fallback)
  -> per chunk: DSPy TranslateMarkdownChunk on the batch LLM profile
       check: non-empty, same heading count, length ratio in bounds,
              English when >= TRANSLATE_CHUNK_LANGUAGE_MIN_CHARS; 1 retry
  -> append to <target>.tmp, heartbeat every TRANSLATE_HEARTBEAT_SECONDS
  -> whole file must detect as English
  -> existing target -> <target>.bak ; os.replace(tmp, target)
  -> DB transaction: switch source row + mark decision
       inside it: master.set_translation + master.flush
     COMMIT
  -> delete .bak, audit translated
```

Modules:

| Module                                                   | Role                                                                                                      |
| -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `src/batch/Main_translate_markdown.py`                   | CLI, wiring, exit codes                                                                                   |
| `src/pipelines/translate/translator.py`                  | `TranslateMarkdownChunk` signature, `check_chunk`, `MarkdownTranslator` (retry, connection-error mapping) |
| `src/pipelines/translate/orchestrator.py`                | run loop, temp/backup files, commit and rollback, progress                                                |
| `src/pipelines/translate/models.py`                      | `TranslatedChunk`, `TranslationItem`, `TranslationAuditRecord`, outcomes                                  |
| `src/pipelines/rag/pgvector/translation_repository.py`   | queue, `set_language`, `commit_translation(before_commit=...)`, audit                                     |
| `src/pipelines/download/descriptor_store.py`             | `translation_entry` / `set_translation` (replayed if the master file changed on disk)                     |
| `src/pipelines/utils/TokenSizeTargetMarkdownSplitter.py` | optional `separators` for the oversized-block fallback                                                    |

**Chunking.** The splitter keeps header context and merges small sections. A single block larger than the chunk size falls back to a recursive split; the translator passes paragraph, line, `". "`, `"; "`, `": "` before space and character, with the separator kept at the chunk end, so cuts land between sentences. Ingestion keeps LangChain's default separators.

**LLM answer shape.** The signature's output is the pydantic `TranslatedChunk`. When the model replies with bare Markdown instead of the JSON object (seen on `qwen3-coder:30b`), DSPy hands back the string; a `model_validator(mode="before")` accepts it (and a JSON string) as the `markdown` field.

## 4. Failure and rollback

| Failing step | Undone | Audit `reason` |
|---|---|---|
| input file missing | nothing written | `input_missing` |
| language not detectable (text too short) | nothing written; outcome `skipped`, not a failure | `language_undetermined` |
| language without a name in `TRANSLATE_LANGUAGE_NAMES` | nothing written | `unsupported_language` |
| a chunk fails the checks twice | `.tmp` deleted | `chunk_rejected` |
| LLM unreachable (connection, timeout, 503) | `.tmp` deleted | `connection_error` |
| any other chunk error | `.tmp` deleted | `unexpected_error: ...` |
| result not detected as English | `.tmp` deleted | `not_english` |
| master flush fails (inside the transaction) | DB rolled back, target removed or `.bak` restored, master entry restored | `commit_failed` |
| DB commit fails after the flush | target removed or `.bak` restored, master entry restored and flushed | `commit_failed` |
| Ctrl-C / SIGTERM | current item undone as above, then the run stops | (no row for the interrupted item) |

Only `TRANSLATE_MAX_CONSECUTIVE_CONNECTION_ERRORS` (default 3) consecutive `connection_error` items abort the run; any other outcome resets the counter. Exit code 1 when any item failed or the run aborted.

## 5. Usage

From the repo root:

```bash
# what would be translated (no LLM call, no writes)
.venv/bin/python -m batch.Main_translate_markdown --dry-run

# translate everything not yet marked language_translated = 'en'
.venv/bin/python -m batch.Main_translate_markdown

# re-translate one decision from its original
.venv/bin/python -m batch.Main_translate_markdown --mode force --ids CELEX-31971D0375

# at most 5 items
.venv/bin/python -m batch.Main_translate_markdown --limit 5
```

The LLM is the batch profile (`BATCH_LLM_PROVIDER` / `BATCH_LLM_MODEL_ID`, falling back to `LLM_MODEL_ID`) on the DGX Ollama server or an external provider, never the local GPU. Prerequisite: `migrate_document_translation.sql`, then `schema_comments.sql` (the CLI exits 2 otherwise).

Measured 2026-10-31: `CELEX-31971D0375` (5.2 KB German, 10 chunks) in 20 s on `qwen3-coder:30b`.

## 6. Settings

| Setting | Default | Meaning |
|---|---|---|
| `TRANSLATE_CHUNK_TOKENS` | 1000 | splitter chunk size (tiktoken tokens) |
| `TRANSLATE_LENGTH_RATIO_MIN` / `_MAX` | 0.5 / 2.0 | accepted `len(translation) / len(source)` per chunk |
| `TRANSLATE_CHUNK_RETRIES` | 1 | extra attempts per rejected chunk |
| `TRANSLATE_CHUNK_LANGUAGE_MIN_CHARS` | 300 | translated chunks this long must detect as English (catches an echoed chunk) |
| `TRANSLATE_HEARTBEAT_SECONDS` | 60 | in-document progress line interval |
| `TRANSLATE_MAX_CONSECUTIVE_CONNECTION_ERRORS` | 3 | abort threshold |
| `TRANSLATE_TARGET_SUFFIX` | `_EN` | output file suffix |
| `TRANSLATE_SPLIT_SEPARATORS` | `\n\n`, `\n`, `. `, `; `, `: `, ` `, `` | fallback separators (code only) |
| `TRANSLATE_LANGUAGE_NAMES` | DE, FR, IT, NL, ES, PT | code -> name given to the LLM (code only) |

## 7. Audit

`celex_facts.document_translation_audit` is append-only, without foreign keys: `run_id`, `decision_id`, `document_id`, `source_file_md`, `target_file_md`, `source_language`, `model_id`, `chunk_count`, `input_tokens`, `output_tokens`, `duration_ms`, `outcome` (`translated`, `skipped`, `already_english`, `failed`), `reason`, `created_at`.

```sql
-- outcome per run
SELECT run_id, outcome, count(*), round(avg(duration_ms)) AS avg_ms
FROM celex_facts.document_translation_audit
GROUP BY run_id, outcome ORDER BY min(created_at) DESC;

-- why decisions failed
SELECT reason, count(*) FROM celex_facts.document_translation_audit
WHERE outcome = 'failed' GROUP BY reason ORDER BY count(*) DESC;
```

## 8. The 2026-10-31 repair (ISSUES 20261031)

Before this batch existed, 81 non-English source rows already pointed at an English `mineru/auto/<id>_en.md` from the old pdf-pytesseract pipeline. For 54 pre-1973 decisions (German PDF only), `Main_backfill_markdown_paths` had copied the source's PDF path and sha256 onto the selected `_EN` candidate row (never downloaded). The first MinerU run converted that German PDF into the candidate's Markdown, and the orchestrator, which matched master entries by PDF path only, switched master `txt_md_file` to the German output.

- Root cause fixed in `pipelines/convert/orchestrator.py`: master is synced from `is_source` rows only, and a candidate sharing its source's PDF is skipped (`skipped_shares_source_pdf`).
- Data repaired by `batch.utils.repair_translated_markdown --apply` (dry run by default): 54 master entries back to `_en.md`, `original_file_md` set to the German `mineru/<id>.md` (registry and master), the 54 phantom candidates' copied file columns cleared (rows kept), `language_translated = en` for all 81 decisions. Master backup `master_doc_descriptors.json.bak-20261031-200429`.

## 9. Known limitations

- 27 of the 81 already translated decisions have no MinerU original yet, so `original_file_md` is NULL; a forced re-translation detects their English input and audits `already_english` instead of translating it. The next lazy MinerU run converts their PDFs into `mineru/<stem>.md` (a translated source row is converted into `original_file_md` or that default path, never into its English `local_file_md`, and master is not touched), but does not yet set `original_file_md`.
- The 16 source rows without a language use the old `auto/<id>.md` layout; 15 are English (a lazy run stores `EN` and audits `already_english`), `AT_40940_10539411_1520_6` is a 161-character closure notice, too short to detect, and is audited `skipped` / `language_undetermined` (exit code stays 0).
- `input_tokens` / `output_tokens` stay NULL (no usage plumbing yet).
- A decision without a master entry is committed registry-only with a warning.
- Translation quality is checked structurally (headings, length, language), not semantically.
