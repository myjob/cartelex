'20261121

a fine-tuned smaller model can match accuracy on a narrow extraction task like yours. But it's probably not your biggest lever. With these models on the Spark, "hours per document" usually means you're making too many calls or generating too many tokens, not that the model is too slow.

## Where the hours probably go

Here's a rough estimate based on your setup (~400-token chunks, per-chunk calls, qwen3-coder:30b at ~80 tok/s decode and ~1,400 tok/s prefill):

- A long Commission decision is about 50K tokens, so roughly 125 chunks.
- If the 50 facts are extracted in separate passes, or by fact type per chunk, plus atomic verification calls, that's thousands of calls per document.
- Each call is about 1.5K prompt tokens (DSPy instructions plus MIPROv2 demos), so about 1s of prefill. Then about 200 output tokens, so about 2.5s of decode.
- 5,000 calls × 3.5s ≈ 5 hours.

That matches what you're seeing. Decode time and call count dominate, and a faster model only cuts the per-call constant.

## Levers, ordered by speedup per unit of accuracy risk

**1. Run calls concurrently (no accuracy risk, often 3–8× throughput).** Decode on the Spark is limited by memory bandwidth. A single request leaves most of the compute idle, and a batch of 16 generates almost as fast per request as a batch of 1. You currently run `OLLAMA_NUM_PARALLEL=2`. Switching to vLLM or SGLang with continuous batching, or llama-server with more slots, and firing 16–32 chunk calls at once (DSPy's `batch()` or a thread pool) is the cheapest win. With an MoE model at ~3B active parameters, the KV cache is the main memory cost, so give it room.

**2. Check for Qwen3 thinking mode (no accuracy risk if the task doesn't need it).** If reasoning is on anywhere, each call may be emitting 500–2,000 hidden tokens. Extraction rarely benefits from it. This alone can be a 5–10× difference.

**3. Cut output tokens (no risk, and better for provenance).** You currently have the model quote supporting sentence fragments. Instead, number the sentences in the chunk and have the model return sentence IDs plus compact typed fields under constrained JSON (a grammar or `response_format`). Then rebuild the quotes in code. That's fewer tokens, exact spans, and no paraphrased "quotes" slipping past your verifier.

**4. Make fewer calls (low risk, often the biggest single win).**

- Extract all fact types in one pass per chunk with a sparse schema, instead of one pass per fact type.
- Pre-route chunks before calling the LLM. Commission decisions are highly structured: fines, addressees, duration and leniency sit in predictable sections. A BM25/regex/embedding filter can skip most chunks for most fact types. This is your router/dispatcher idea applied at ingestion time.
- Only send borderline cases to the entailment verifier. Let clear matches pass on cheaper checks.

**5. Use prefix caching (no risk).** Put the static instructions and demos first and the chunk last, so every call reuses the same cached prefix. vLLM does this automatically. With llama-server it only works if the prefix is byte-identical, so check that DSPy isn't reshuffling demos.

**6. Fine-tune a smaller model (moderate effort, accuracy can be equal or better).** Now to your actual question. You're in an unusually good position for distillation. Your pipeline has already produced facts across ~740 documents, and those facts passed a hard verification gate. That's a large training set of verified (chunk, output) pairs, far more than the ~50 examples MIPROv2 works from.

A LoRA fine-tune of a Qwen3-4B or 8B on those pairs gives you:

- A model that knows your schema and taxonomy without few-shot demos, so prompts drop from ~1.5K to ~500 tokens.
- Faster decode and much more headroom for batching.
- On narrow, closed-taxonomy extraction, a distilled small model often matches its teacher.

What it costs:

- You need a held-out gold set to prove you haven't lost accuracy. Your 50 labeled examples are too few for that. Aim for 200+ hand-checked chunks, weighted toward the hard cases (Art. 101 vs. 102 confusion, pre-2009 article numbering, relative dates).
- Any bias in the teacher gets baked into the student. Keep the big model as the verifier at first, so the fine-tuned model only proposes candidates and the existing gate still decides.

**7. Quantize (small risk).** FP8 or NVFP4 on the Blackwell chip gives a modest speedup. Run it against the same gold set.

## What I'd do first

Measure before changing models: calls per document, average prompt and output tokens, and whether thinking is on. In most cases, levers 1–4 get you from hours to tens of minutes with the model you have, and fine-tuning gets you the rest of the way. Fine-tuning a model that's being called 5,000 times sequentially only fixes the cheapest part of the problem.

If you send me the call counts and token stats for one slow document, or the DSPy extraction module, I can tell you which of these applies.


## Extract all fact types in one pass per chunk with a sparse schema, instead of one pass per fact type.

Right now each chunk is probably read several times, once per fact type or per small group of facts. Every one of those calls pays the full prefill again for the same ~400 chunk tokens plus the instructions, and each call usually comes back empty because most fact types don't appear in most chunks.

A single pass flips that around. The model reads each chunk once and returns only the facts that are actually in it. With 125 chunks and 50 fact types, that's 125 calls instead of up to 6,250.

## "Sparse" is the key word

There are two ways to build a one-pass schema, and only one of them actually saves time.

**Dense (avoid this):** a fixed object with 50 fields, most of them null.

```json
{"fine_amount": null, "infringement_start": null, "leniency_applicant": null, ... 47 more}
```

The model has to generate every key and every `null`, so you pay decode cost for facts that aren't there. At ~80 tok/s that's several seconds of nothing per chunk.

**Sparse (do this):** a list of typed fact records, where facts that aren't present simply don't appear.

```json
{"facts": [
  {"type": "fine_amount", "company": "Saint-Gobain", "value": "896000000", "currency": "EUR", "sentence_ids": [14]},
  {"type": "infringement_start", "date": "1998-03", "sentence_ids": [9, 10]}
]}
```

An empty chunk returns `{"facts": []}`, which is a handful of tokens. Output size now tracks how much fact content the chunk holds, not how big the schema is.

## In DSPy

Use a Pydantic discriminated union so each fact type keeps its own validated fields:

```python
from typing import Literal, Union, Annotated
from pydantic import BaseModel, Field
import dspy

class FineAmount(BaseModel):
    type: Literal["fine_amount"]
    company: str
    value: str
    currency: str
    sentence_ids: list[int]

class TemporalEvent(BaseModel):
    type: Literal["temporal_event"]
    role: Literal["infringement_start", "infringement_end",
                  "decision_date", "leniency_application_date"]  # your closed taxonomy
    date: str            # EDTF, normalised later in code
    actor: str | None
    sentence_ids: list[int]

Fact = Annotated[Union[FineAmount, TemporalEvent, ...], Field(discriminator="type")]

class ExtractFacts(dspy.Signature):
    """Extract every fact of the listed types stated in the chunk.
    Return an empty list if none are present. Cite sentence numbers only."""
    chunk: str = dspy.InputField(desc="sentences prefixed [1], [2], ...")
    facts: list[Fact] = dspy.OutputField()
```

Pair this with constrained decoding: JSON schema or a grammar in vLLM or llama-server. That way malformed output can't happen, and you don't need retries.

## Where accuracy can slip

Putting everything in one call isn't free. These are the usual failure modes:

1. **Recall drops for rare fact types.** With 50 types in one instruction, the model focuses on common, salient facts like fines and dates and quietly skips rarer ones like leniency reductions or recidivism uplifts. This is the main risk.
2. **The schema bloats the prompt.** 50 type definitions with field descriptions can come to 2–3K tokens. Prefix caching mostly cancels the cost, but the attention dilution is real.
3. **Fields of related types get mixed up.** Similar types, such as a basic fine amount and the final fine after adjustments, get swapped more often when they sit side by side.

## Mitigations

- **Group the facts into 4–6 families instead of one mega-schema.** For example: parties and addressees, temporal events, fines and calculation, procedure (leniency, settlement, appeals), legal basis (articles, market definition). Each family gets one pass per chunk. That's still about 10× fewer calls than one pass per fact, while keeping each prompt focused. In practice this is usually the right setting.
- **Route families to chunks.** Combine this with the pre-routing lever, so the fines family runs only on chunks that a cheap BM25 or embedding filter flags as fine-related. Most chunks then get 1–2 family passes, not 6.
- **Check recall per fact type, not just overall F1.** Run old and new extraction on the same 20–30 documents and compare recall for each type separately. Overall numbers will hide a rare type that has dropped to zero. Any family that regresses can be split further.
- **Keep the verifier exactly as it is.** Each emitted record is still one atomic proposition with sentence IDs, so `EvidenceSupportResult` gates it the same way as before. Single-pass extraction only changes how candidates are proposed, not how they're accepted.

## Merge across chunks in code, not in the LLM

The same fact often shows up in several chunks: the summary, the body, and the operative part. With per-chunk extraction you'll get duplicates and the occasional conflict. Resolve these deterministically after extraction:

- Use a normalised key such as `(type, company, role)` to deduplicate.
- Keep every `sentence_ids` citation, because multiple supporting sources strengthen provenance.
- Flag real conflicts for review instead of letting the model choose. An example is two different infringement start dates for the same company. The operative part of the decision usually takes precedence, and you can encode that as a rule.

This also avoids the kind of duplicate-timeline bloat that caused your earlier KV-cache incident. Deduplication happens once, in SQL or Python, and never in a prompt.

## What to expect

Going from per-fact-type passes to family passes with routing typically cuts the call count by 10–30×. Each call gets slightly longer, but prefix caching absorbs most of that. On top of running calls concurrently, that's realistically the difference between hours and minutes per document, with no change of model.