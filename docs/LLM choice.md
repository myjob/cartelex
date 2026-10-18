#adr 

Let me check what's actually shipped in the Qwen3.5/3.6 coder line, since that affects what you can pull.Two things, and the second matters more than the first.

On the qwen3.5-coder hope: there isn't really an official first-party one. Qwen3.5 shipped in February 2026, and Qwen3.6 is the current open-weight release of that line — but those are unified vision-language hybrid models with thinking, not a dedicated coder. What you'll find on Ollama under "qwen3.5-coder" are community repackages — e.g. variants that take the official Qwen3.5 models and just tune the sampling parameters (lower temperature, presence penalty) for more deterministic code output, with vision retained. So it's the same base model with coding-friendly defaults, not a new coder checkpoint. And both 3.5 and 3.6 are hybrid thinking models — you disable reasoning by passing `enable_thinking=False` (instruct mode), so they're "no reasoning" only when you actively switch it off.

But here's the real point: **for legal-text QA/extraction you shouldn't be on a coder model at all.** A coder tune is optimized for deterministic code generation; what your celex pipeline wants is strong instruction-following, long-context comprehension, and clean structured output — that's an **instruct** model's job. You've been comparing within the wrong family.

The natural upgrade that keeps everything you like about your current setup:

**Qwen3-Next-80B-A3B-Instruct** — 80B total but still only ~3B active per token, so it runs in the _same speed class_ as your qwen3-coder:30b (which is 30B total with ~3.3B activated), just with much more capacity for legal prose, citation handling, and extraction. It's a pure instruct model — non-thinking by default, text-only, no trace bleed to parse out in DSPy. On 128 GB you've got enormous headroom: your current model is only ~19 GB, so jumping to an 80B-A3B instruct (~45 GB at Q4) still leaves plenty of room for a big context window. This is the single best swap for your use case.

If you want the higher quality ceiling for the genuinely hard analytical questions and can accept slower decode, **GLM-4.5-Air** (~12B active) is the "hard task" fallback — keep it alongside, route to it only when the 80B-A3B isn't enough. I'd skip the 235B-class instruct models; at ~22B active they crawl on the Spark's 273 GB/s and only make sense for offline batch runs.

And if you specifically want current-gen and don't mind running a hybrid model in non-thinking mode, **Qwen3.6-35B-A3B** with `enable_thinking=False` is a reasonable middle option — newest comprehension, 3B active — but it carries vision weights you don't need and a thinking path you have to keep switched off, which is why for a clean text-only extraction pipeline I'd still lead with the pure Qwen3-Next instruct.

Want me to sketch the Modelfile / `num_ctx` and sampling settings for Qwen3-Next-80B-A3B-Instruct tuned for extraction (low temperature, no thinking), so it drops into your DSPy `dspy.LM()` setup?


## Ollama support

Let me check the Ollama-specific support directly, since that's your actual question.Good instinct to check — there are two separate things going on here.

It does exist on Hugging Face; you were likely searching the wrong name. It's **Qwen3-Next** (the September 2025 series), not "Qwen3.5-Next." The official repos are `Qwen/Qwen3-Next-80B-A3B-Instruct` and a GGUF build at `Qwen/Qwen3-Next-80B-A3B-Instruct-GGUF`, plus Unsloth's `unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF`. It's an 80B MoE with Instruct and Thinking variants, built on a new hybrid architecture (MoE + Gated DeltaNet + Gated Attention) that optimizes for fast long-context inference — roughly 10× faster than Qwen3-32B. The instruct variant is non-reasoning, which is what you want.

The Ollama answer is messier. The qwen3-next architecture _was_ supported — there used to be a working `qwen3-next:80b` tag — but there's a **known regression**: after Ollama v0.18.x the model fails to load with `qwen3next: layer 0 missing attn_qkv/attn_gate projections`. The same loader bug bites the related Qwen3.6-35B-A3B MTP GGUFs, which abort with `qwen3next: layer 40 missing attn_qkv/attn_gate projections`. So whether it runs depends on your Ollama version, and on a current build it may well be broken. That novel Gated DeltaNet attention is exactly the kind of thing Ollama's engine handles unreliably.

The reliable route for this model is **llama.cpp directly**. The `qwen_next` support is fully merged upstream (PR #16095, in llama.cpp release b7186 and later), and Unsloth ships tested GGUFs with documented sampling settings. Spin up `llama-server`, which exposes an OpenAI-compatible endpoint — that drops into your DSPy setup the same way Ollama does (`dspy.LM("openai/...", api_base="http://localhost:8080/v1")`), and you sidestep the Ollama qwen3next breakage entirely. The Unsloth instruct recipe is roughly:

```
llama-server -hf unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF:Q4_K_XL \
  --jinja -ngl 99 --ctx-size 32768 \
  --temp 0.7 --min-p 0.0 --top-p 0.80 --top-k 20 --presence-penalty 1.0
```

On your Spark, Q4 lands around ~45 GB with only ~3B active per token, so it's the same fast speed class as your current qwen3-coder:30b but with much more capacity for legal prose and extraction.

One aside that clears up your first message: there _is_ a `Qwen3-Coder-Next` (the 80B-A3B coder sibling, also non-reasoning) — that's probably what `qwen3-coder-next:latest` referred to. But since your work is legal-text QA, stick with the **-Instruct**, not the coder.

If you'd rather not leave Ollama, the realistic fallbacks are pinning a pre-0.18 Ollama (ugly) or using a clean instruct MoE that Ollama handles well today — `qwen3.6:35b` loads fine via the official tag (it's the MTP/Unsloth GGUF variants that break), and you'd run it with thinking disabled.


https://ollama.com/library/qwen3.6:35b
https://ollama.com/library/qwen3-next


- [ ] #medium feature 20260625: benchmark  qwen3.6:35b vs qwen3-coder:30b  
[implemented in llm_benchmark_test]( src/pipelines/utils/tests/llm_benchmark_test.py)
[implemented in test_vertical_restraints_integration]( src/pipelines/utils/tests/test_vertical_restraints_integration.py)

PYTHONPATH=src .venv/bin/python -m unittest src.pipelines.utils.tests.test_vertical_restraints_integration -v

--------------------------------------------------------------------------------
INTEGRATION  CELEX-31990D0645 / infringement_subcategory_vertical_restraints
--------------------------------------------------------------------------------
  Baseline  qwen3-coder:30b             19.6s  source=hybrid  refs=[5, 9, 11]
  Answer:   Territorial Exclusivity / Parallel Trade Bans
  Candidate qwen3.6:35b                 77.8s  source=hybrid  refs=[9, 6, 10]
  Answer:   Territorial Exclusivity / Parallel Trade Bans
  Faster model: qwen3-coder:30b
--------------------------------------------------------------------------------
# Conclusion 
newer qwen does not perform better and (simple) questions are answered by the same responses
