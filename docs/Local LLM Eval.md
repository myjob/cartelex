# Local LLM Eval

Comparison of local Ollama models (NVIDIA DGX Spark) as the batch fact-extraction
LLM (`settings.BATCH_LLM_MODEL_ID`, falling back to `settings.LLM_MODEL_ID`).
Related: [LLM choice](LLM%20choice.md), [Design BgeRanker](Design%20BgeRanker.md).

## Motivation (2026-11-07)

During the fp16 reranker A/B, re-running the *identical* `UniversalFactModule.extract`
prompt gave a different extraction in 48/176 cases (27.3%) at `LLM_TEMPERATURE=0.0`
(`reports/reranker_fp16_ab_extract_20261107.csv`). The model in that run was
**`qwen3-coder:30b`** -- the `LLM_MODEL_ID` default -- not `qwen3-coder-next`:
`BATCH_LLM_MODEL_ID` is unset in `settings.py`, `config_main/.env` and the shell.

## Candidates

| Model | Ollama tag | Params | Quant | Size |
|---|---|---|---|---|
| Qwen3-Coder 30B-A3B (current default) | `qwen3-coder:30b` | 30.5B | Q4_K_M | 18.6 GB |
| Qwen3-Coder-Next 80B-A3B | `qwen3-coder-next:latest` | 79.7B | Q4_K_M | 51.7 GB |
| GLM-4.7-Flash | `glm-4.7-flash:latest` | 29.9B | Q4_K_M | 19.0 GB |
| Qwen3.5 35B | `qwen3.5:35b` | 36.0B | Q4_K_M | 23.9 GB |
| Qwen3.5 122B | `qwen3.5:122b` | 125.1B | Q4_K_M | 81.4 GB |

## Method

- **Prompt set (fixed):** 100 prompts = 25 `universal` facts x 4 decisions, drawn
  (seed 11) from the 47 decisions of the reranker A/B sample whose text exceeds
  `SMALL_DOC_CONTEXT_TOKEN_LIMIT` (12k tokens), i.e. where production uses hybrid
  retrieval. Contexts were built once with the production `HybridSearch`
  (`HYBRID_SEARCH_TOP_K=50`, fp16 BGE reranker) and stored, so every model sees
  byte-identical prompts. Context size: min 963 / median 11,144 / max 13,235 tokens.
- **Call path:** one process per model with `BATCH_LLM_MODEL_ID=<tag>`, i.e. the
  production `batch_profile()` -> `create_lm()` path; `UniversalFactModule.extract`
  (the module's own DSPy predictor); uncached; `LLM_TEMPERATURE=0.0`; thinking
  disabled (verified: no reasoning content, 96-163 completion tokens per answer).
- **Two passes:** pass 1 over all 100 prompts, then pass 2, so a repeat never runs
  directly after its twin (as in production, where different facts alternate).
- **Metrics:**
  - *repeat_same_strict* -- pass 1 and pass 2 give byte-identical JSON.
  - *repeat_same_value* -- identical after ignoring key names of single-key
    dicts, case, whitespace and trailing punctuation (separates format noise from
    answer churn).
  - *schema_violation* -- share of answers whose dict keys differ from the fact's
    `output_schema` (or the fact name when the schema is empty), e.g. `value`.
  - *bool_vocab_violation* -- answers to True/False/NA facts that are not exactly
    the string `True`, `False` or `NA` (e.g. `Yes`, a bare JSON boolean).
  - *grounded_verbatim / grounded_names* -- share of extracted string values
    found verbatim (case/whitespace-folded) in the context; `grounded_names` over
    name-type facts (companies, applicants, commissioner, fines), where a correct
    value is almost always verbatim. A proxy for invented values, not accuracy.
  - *matches_majority_of_others* -- pass-1 value equals the plurality answer of
    the other four models (when that plurality has >= 2 votes). Agreement, not
    ground truth.
  - latency median / p90 per call.
- **Not measured:** accuracy against a hand-labelled gold set (none exists).

## Results

1000 calls (5 models x 100 prompts x 2 passes), all thinking-free (reasoning 0 chars).
All 14 errors are `LMTimeoutError` at `OLLAMA_TIMEOUT=150` s, none are parse failures.

| Model | repeat same | errors | empty | schema viol. | bool vocab viol. | grounded (all / names) | agrees w/ others | sec median / p90 |
|---|---|---|---|---|---|---|---|---|
| `qwen3-coder:30b` | **1.00** | **0** | 34 | 4.8% | **0** | 0.833 / **0.986** | 0.707 | 10.7 / **15.2** |
| `qwen3-coder-next` | 1.00 | 2 | 48 | 12.0% | 12 | 0.735 / 0.837 | 0.667 | 14.0 / 26.3 |
| `glm-4.7-flash` | 0.73 (25/92 clean pairs differ) | 10 | 32 | **2.5%** | 4 | 0.841 / 0.928 | 0.694 | 11.4 / 23.2 |
| `qwen3.5:35b` | 1.00 | 0 | 44 | 14.1% | 2 | 0.820 / 0.903 | **0.756** | **9.6** / 25.6 |
| `qwen3.5:122b` | 1.00 | 2 | 56 | 38.0% | 0 | **0.861** / 0.804 | 0.725 | 23.8 / 41.8 |

Raw: `reports/local_llm_eval_20261107.csv` (summary), `_pairwise.csv`, `_per_fact_unstable.csv`,
`_b2b.csv` (back-to-back test below). Pairwise pass-1 agreement between any two models is only
0.46-0.62 (highest: qwen3.5:35b vs 122b), so the facts themselves are ambiguous on these
large documents; agreement with the others is a weak signal, not accuracy.

### Findings

1. **The variance is not a property of `qwen3-coder:30b`, it comes from serving state.** When
   the twin call came ~100 prompts later, every Qwen model reproduced 100/100 answers
   byte-for-byte. A follow-up ran the twin *immediately* after the first call, as the fp16 A/B did,
   for 40 prompts on `qwen3-coder:30b`: 13/40 (32.5%) differ. The second call took 5.5 s against
   10.1 s, which shows that Ollama (llama.cpp) reused the cached KV prefix of the identical prompt. Twin 1
   also differs from the earlier spaced pass-1 answer in 18/40 cases. All prompts share
   the long DSPy instruction prefix, so the result depends on what the server evaluated before
   (cache reuse length, batch shapes, float summation order) and greedy decoding flips near-ties.
   The two spaced passes matched only because they ran the identical call sequence.
   **Changing the model does not remove this.** It reproduces the 27.3% of the A/B.
2. **`glm-4.7-flash` is intrinsically unstable.** Even under the call sequence that is
   reproducible for every Qwen model, 25/92 answers changed: a True/False flip, NA vs False,
   dropped entities, and mojibake (`EspaÒa`) in one pass only. It also had 10 timeouts. Rejected.
3. **Key contract (`schema viol.`).** For `output_type: str` facts the module asks for
   `{"value": ...}` (`_STR_SCHEMA`). Every model sometimes writes the fact name instead.
   `qwen3.5:122b` does so most (38%) and also renames dict keys (`companies` -> `company`,
   `products` -> `product`, `result`). `qwen3-coder:30b` is the most compliant Qwen model (4.8%).
4. **Boolean vocabulary.** `qwen3-coder-next` answers `Yes`/`No` or similar instead of `True`/`False`/`NA`
   in 12 classification answers, and `qwen3.5:35b` returns a bare JSON boolean twice.
   `qwen3-coder:30b` and `qwen3.5:122b`: 0.
5. **Grounding of names.** `qwen3-coder:30b` copies company/applicant/fine names verbatim
   98.6% of the time. The others paraphrase or invent more often (80-93%). Since names are joined
   against the entity graph, this is the metric with the most downstream weight.
6. **Empty answers that every model shares.** `fines` (3/4 prompts), `leniency_applicants`
   (3/4), `services` (3-4/4) and `exemption_applicants` are empty for *all five* models on the
   same decisions. This points at retrieval or the question, not the model: 50 hybrid chunks of an
   ~12k-token context apparently miss the fines article.
7. **Latency.** `qwen3.5:35b` has the lowest median but a long tail. `qwen3-coder:30b` has the
   tightest p90 (15 s), and `qwen3.5:122b` is 2.2x slower.

## Recommendation

**Keep `qwen3-coder:30b` (no change to `LLM_MODEL_ID` / `BATCH_LLM_MODEL_ID`).** None of
the candidates fixes the variance (finding 1). `qwen3-coder:30b` is best or tied on errors,
boolean vocabulary, name grounding and tail latency, and second on the key contract. Its only
weakness is consensus with the other models, where `qwen3.5:35b` leads (0.756 vs 0.707).
`qwen3.5:35b` is the only candidate worth a second look, and only against a hand-labelled gold
set, since it costs more schema and vocabulary violations and more empties.
`qwen3-coder-next`, `glm-4.7-flash` and `qwen3.5:122b` are not recommended.

To act on the variance itself (model-independent):
- Normalise the output key in code: accept the fact name as an alias of `value` for
  `output_type: str`. This removes the pure format-noise share of the "changes"
  (in the fp16 A/B, 7 of 18 diffs were the same value with another key, case or comma).
- Self-consistency (n=3 majority) for True/False/NA facts, where a flip is a real error.
- A fixed `seed` does not help with greedy decoding; the source is cache and batch numerics. Do not
  measure LLM noise with back-to-back identical calls, since the cache hit inflates it.

## Reproduce

Scripts are in the session scratchpad (not committed): `llm_build_prompts.py`
(prompt set), `llm_run.py` (one model, two passes, resumable JSONL), `llm_analyze.py`
(metrics), and `llm_b2b.py` (back-to-back twin calls). Raw results: `reports/local_llm_eval_20261107*.csv`.
