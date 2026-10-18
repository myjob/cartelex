# CLI flags must overrule FACT_EXTRACTOR.yaml run-state

## Context

The reported symptom is real, but the cause is not the one named in the issue. This command silently does nothing:

```
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
  --celex-list-file reports/corp-investigated_empty.txt \
  --filter-fact companies_investigated \
  --overwrite
```

Tracing it:

- `--overwrite` **is** wired up. It flows `run()` -> `PipelineExecutor(overwrite=...)` ([Main_ingest_generic_facts.py:334](src/batch/Main_ingest_generic_facts.py#L334), [:159](src/batch/Main_ingest_generic_facts.py#L159)) -> `FactExtractorParamVO.from_yaml_struct`, where it is merged with the YAML value by boolean OR at [fact_extractor_param.py:88](src/pipelines/agent_dspy/fact_extractor_param.py#L88). It then gates the DB existence check at [universal_fact_extraction_pipeline.py:86](src/pipelines/agent_dspy/universal_fact_extraction_pipeline.py#L86) and the per-module cache lookup at [universal_modules.py:161](src/pipelines/agent_dspy/universal_modules.py#L161). So `force_override` already behaves as the issue asks.
- The real blocker is `run_state_active`. `_filter_active_structs` drops inactive configs *before* applying `--filter-fact` ([Main_ingest_generic_facts.py:481](src/batch/Main_ingest_generic_facts.py#L481)), so a fact with `run_state_active: false` is invisible to the filter, `:495` returns `[]`, and `run()` exits at `:317` with "No active facts matched criteria". `companies_investigated` is exactly that case ([FACT_EXTRACTOR.yaml:358-386](config_main/FACT_EXTRACTOR.yaml) — `force_override: false`, `run_state_active: false`), as are all facts except `case_commissioner`.

So `--overwrite` looks inert because the run never selects a single fact. The fix is feasible and small: make `--filter-fact` an explicit intent that overrules `run_state_active` for the requested fact and its recursive predecessor closure. The `--overwrite` half needs no code change, only documentation.

**Decided scope** (confirmed with the user):
- `--filter-fact X` activates X plus its full recursive predecessor closure, regardless of each entry's `run_state_active`. `--overwrite` then applies to that whole selected set (matching the intent already written at [FACT_EXTRACTOR.yaml:9](config_main/FACT_EXTRACTOR.yaml#L9)).
- `--overwrite` **without** `--filter-fact` keeps today's semantics: it forces override only across facts that are already `run_state_active: true`. It must never activate the whole 40+ fact catalogue.

## Implementation

### 1. `src/batch/Main_ingest_generic_facts.py` — `_filter_active_structs` ([:477-509](src/batch/Main_ingest_generic_facts.py#L477-L509))

Keep the existing two-argument signature (`structs`, `filter_fact`) so the seven existing call sites in tests stay valid. Change only the candidate set:

- When `filter_fact` is falsy: unchanged — `[s for s in structs if s.get("run_state_active", True)]`.
- When `filter_fact` is given: skip the `run_state_active` filter entirely and build `struct_by_name` over **all** structs. The closure walk in `_collect_fact_with_predecessors` ([:511-530](src/batch/Main_ingest_generic_facts.py#L511-L530)) then resolves inactive predecessors instead of warning and dropping them, and the final `selected_structs` comprehension already narrows the result to the closure, so unrelated inactive facts still never run.

Add an explicit INFO log naming the entries whose `run_state_active` was overruled, e.g.
`"--filter-fact overrules run_state_active for: %s"` listing the selected names that carry `run_state_active: false`. Silence here is what made the original bug hard to see; the log is the main usability payoff.

Extract the candidate selection into a small helper (`_candidate_structs(structs, filter_fact)`) if `_filter_active_structs` grows past a clean single responsibility — the module already follows that topic-centric split.

Note the ordering constraint: `_normalize_predecessors` and `_index_structs_by_target` must run over the same widened candidate list, otherwise the closure walk cannot see the inactive predecessors it now needs.

### 2. Documentation of the flags (same file)

- `--overwrite` help text at [:343-345](src/batch/Main_ingest_generic_facts.py#L343-L345): state that it ORs with the per-fact `force_override`, and that it cannot switch a YAML `force_override: true` back off.
- Module docstring / usage block near [:1048](src/batch/Main_ingest_generic_facts.py#L1048): document that `--filter-fact` overrules `run_state_active` for the fact and its predecessors, and add the issue's command as a worked example.
- Leave `BatchJobRunner._setup_default_args` ([BatchJobRunner.py:62-67](src/batch/BatchJobRunner.py#L62-L67)) untouched — `--filter-fact` is shared with other `Main_*.py` scripts that never read this YAML, so the run-state note belongs in the ingest script, not the shared parser.

No change to `fact_extractor_param.py`, `universal_fact_extraction_pipeline.py`, or `universal_modules.py`.

### 3. Tests — `src/batch/tests/test_Main_ingest_generic_facts.py`

Extend `TestFactIngestionOrchestratorDependencies` (setUp already builds a real `FactIngestionOrchestrator`, no mocks needed — these are pure list transforms):

- `test_filter_fact_activates_inactive_target`: single struct with `run_state_active: false`, `_filter_active_structs(structs, "companies_investigated")` returns that one struct (today: `[]`).
- `test_filter_fact_activates_inactive_predecessors`: target active, predecessor `run_state_active: false` -> both selected, and `_topological_order` puts the predecessor first.
- `test_filter_fact_excludes_unrelated_inactive_facts`: an inactive fact outside the closure is still not selected.
- `test_no_filter_fact_still_drops_inactive`: regression guard for the `--overwrite`-alone decision.

Also add one executor-level test asserting the `--overwrite` merge, which nothing currently covers: build `PipelineExecutor(..., overwrite=True)` with a struct carrying `force_override: false` and assert the `FactExtractorParamVO` handed to `pipe.process` has `force_override is True` (mirror the mock style of `test_process_single_celex_active` at [:38-70](src/batch/tests/test_Main_ingest_generic_facts.py#L38-L70)).

### 4. `ISSUES.md` — mark done and record what the trace unveiled

Close the `- [ ] #med 20260813` entry as `- [x]`, and add on top with prefix `- [ ] #high 20260813`:

- `--overwrite` is a one-way OR: a YAML `force_override: true` (currently `case_commissioner`) cannot be turned off from the CLI. There is no `--no-overwrite`, so that fact is re-extracted on every run.
- `FactIngestor.exists()` ([PipelineFactIngestor.py:155-158](src/tools/PipelineFactIngestor.py#L155-L158)) counts any persisted row as done, so a stored `NA`/empty value blocks re-extraction — even though `_has_value` ([Main_ingest_generic_facts.py:251-260](src/batch/Main_ingest_generic_facts.py#L251-L260)) treats those same values as unfulfilled for predecessor purposes. This is why `reports/corp-investigated_empty.txt` exists.
- `fact_dependency_manager.py:55-60` defaults `run_state_active` to `False` while the orchestrator defaults it to `True`. The module has no production call sites (only `src/batch/tests/`) — candidate for deletion.

Copy this plan to `docs/claude/plans/cli-overrules-run-state-20260813.md` per CLAUDE.md.

## Verification

1. `ruff check src/batch/Main_ingest_generic_facts.py src/batch/tests/test_Main_ingest_generic_facts.py`
2. `.venv/bin/python -m unittest src.batch.tests.test_Main_ingest_generic_facts -v` — all existing tests must still pass unchanged (the no-filter path is untouched).
3. Selection-only smoke test, no LLM calls, confirms the fact is now picked up and logs the override:
   ```
   .venv/bin/python src/batch/Main_ingest_generic_facts.py \
     --filter-celex-id 'CELEX-31989D0408' \
     --filter-fact companies_investigated \
     --overwrite --dry-run
   ```
   Expect `Loaded N sorted fact configurations`, an `Execution sequence:` containing `companies_investigated`, the new run-state-override INFO line, and a `[DRY RUN] Would process fact` line — instead of today's `No active facts matched criteria`.
4. Real end-to-end run on a small slice (LLM traffic goes to the remote Ollama/Spark DGX host per settings):
   ```
   .venv/bin/python src/batch/Main_ingest_generic_facts.py \
     --celex-list-file reports/corp-investigated_empty.txt \
     --filter-fact companies_investigated --overwrite --limit 2
   ```
   Confirm the pipeline does **not** log "Skipping pipeline ... Fact exists", and that `rule_facts.facts` rows for `companies_investigated` on those CELEX ids are rewritten (check `updated_at`/row count before and after).
5. Negative check: run without `--filter-fact` and with `--overwrite` only, and confirm the selection still contains just the `run_state_active: true` facts.
