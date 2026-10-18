 `2026-08-01

For your use case of processing 1000 company profiles, **`deepseek-v4-flash` is the better choice**. Here's why:

## Why Flash works perfectly here:

1. **Structured, well-defined task**: Your prompt is highly specific with clear inputs/outputs. Flash excels at following precise instructions for structured extraction tasks.

2. **Knowledge retrieval, not reasoning**: You're asking the model to recall factual information about companies, not perform complex multi-step reasoning. Flash handles knowledge-based tasks well.

3. **Cost efficiency at scale**: With 1000 companies, Flash's significantly lower cost will save you real money without sacrificing quality for this task type.

4. **Speed matters**: Flash processes faster, which matters when running through 1000 records.

## When you'd need Pro instead:

Only switch to Pro if you notice:
- Companies from very obscure/niche industries where Flash keeps returning "NA" for everything
- Complex disambiguation cases where Flash mixes up similarly named companies
- Situations requiring nuanced judgment (e.g., distinguishing between a company that was "merged" vs "acquired" from subtle context)

## Suggested approach:

Run a small batch test first (maybe 20-30 companies) with Flash. If the accuracy meets your needs, you're golden. If you see too many NAs or errors, then consider Pro for the full run.

Given your pipeline already attempted register lookups (GLEIF, Handelsregister, etc.), the LLM is your fallback—Flash should handle most cases where the company exists in the model's training data.


The short answer: you’re almost certainly using a **reasoning model** (like `deepseek-reasoner`), where the chain‑of‑thought is built‑in and cannot be completely turned off. The `thinking: {type: disabled}` parameter you’re sending is **accepted but ignored** by that model endpoint, so it still generates thousands of reasoning tokens.

Here’s how to fix it and what’s really happening.

---

## Why `thinking: {type: disabled}` doesn’t work for your model

DeepSeek’s API has two main model families:

| Model endpoint | Reasoning behaviour | Controlling reasoning |
|---------------|---------------------|------------------------|
| `deepseek-chat` (standard V3, newer V3.1 etc.) | No chain‑of‑thought by default. *Can optionally* enable “thinking” via `thinking: {type: enabled}`. | Set `thinking: {type: disabled}` to **prevent** any reasoning content. |
| `deepseek-reasoner` (R1) | **Always** produces a chain‑of‑thought in the `reasoning_content` field. | You cannot disable it; you can only control the *effort* via `reasoning_effort`. The `thinking` parameter is ignored silently. |

The key point: **`thinking: {type: disabled}` only works on `deepseek-chat`**. On `deepseek-reasoner`, the model will output `reasoning_content` no matter what you pass, exactly as you’re seeing.

Your model name `deepseek-v4-flash` likely points to a reasoning‑capable model under the hood (maybe a `deepseek-reasoner` variant, or a proxy that routes to it). The API accepts your `thinking` parameter without error because it doesn’t reject unknown fields, but it simply doesn’t apply it.

---

## How to get **zero reasoning tokens** and the latency you expect

**Switch to the non‑reasoning model.**  
For a direct answer without any hidden chain‑of‑thought, use `deepseek-chat`. In LiteLLM that means setting the model identifier to something like:

```python
lm = LiteLLM(model="deepseek/deepseek-chat", ...)
# or if you're using a custom provider route, ensure it resolves to deepseek-chat
```

Once you’re on `deepseek-chat`, you can *either*:

- Omit the `thinking` kwarg entirely – the model won’t reason.
- Or keep `thinking: {type: disabled}` as an extra safety net.

With that change, the completion will only contain `content` (your ~430‑char answer). No `reasoning_content` will appear in the response, and you’ll stop paying for the hidden chain‑of‑thought.

---

## If you *must* use a reasoning model

If the task really benefits from the reasoning model’s deeper analysis and you want to keep it but just reduce the cost:

- Use `deepseek-reasoner` with `reasoning_effort` set to `"low"` (or `"medium"`). That cuts down the number of reasoning tokens, but you will **still get some** `reasoning_content` and pay for it.
- There’s no `"disabled"` option for `deepseek-reasoner`.

---

## Quick sanity check

You can verify the model’s behaviour directly with a minimal `curl` call (using your API key):

```bash
curl https://api.deepseek.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "Say hello in 10 words"}],
    "thinking": {"type": "disabled"}
  }'
```

With `deepseek-chat` you’ll see only `content`; with `deepseek-reasoner` you’ll still see `reasoning_content`.

---

## Summary

- **Problem**: You’re hitting a reasoning model endpoint that ignores `thinking: disabled`.
- **Root cause**: The model name you’re using (`deepseek-v4-flash`) is likely a reasoning model (or an alias for one).
- **Fix**: Switch to `deepseek-chat` (the standard non‑reasoning model).  
- **Result**: No `reasoning_content`, no wasted tokens, and latency drops to what a direct answer actually needs.

If you’re unsure which model your provider is actually routing `deepseek-v4-flash` to, check the API response’s `model` field or ask your provider. Once you’re on `deepseek-chat`, the `thinking` parameter will behave as expected.


-----------
The short answer: **With a typical paid DeepSeek API key, 4–8 concurrent calls (a threadpool of that size) will almost never hit a concurrency limit, and you’re unlikely to hit the rate limit unless you’re making many calls in quick succession over a full minute.** Here’s the breakdown.

---

## DeepSeek’s limits in practice (as of now)

For the standard `deepseek-chat` model, the limits are separate for **concurrent requests** and **requests per minute (RPM)** / **tokens per minute (TPM)**. While exact numbers can change and depend on your account tier, here are the typical public limits:

| Limit type | Typical paid tier value | What it means for you |
|------------|-------------------------|------------------------|
| **Max concurrent requests** | 20–60 (usually 60 for chat) | You can have 60 in‑flight requests at the same time. A threadpool of 4 or 8 is well under that. |
| **Requests per minute (RPM)** | 500–1000 RPM for chat (varies) | 4–8 threads sending one request each per second would be 240–480 RPM, which is fine. If you fire hundreds of requests immediately and sustain them, you might get close to the limit, but normal bursty use won’t. |
| **Tokens per minute (TPM)** | Often 100k–500k TPM | Even with 8 concurrent calls, your token usage would have to be enormous (e.g., max‑output responses) to hit this. Since you’re getting short 430‑char answers, you’ll be far from the limit. |

So **no, a threadpool of 4–8 won’t cause quota or concurrency issues** under normal use. You’ll only see `429 Too Many Requests` if you sustain a very high rate or somehow pile up too many concurrent open connections (but 8 is harmless).

---

## One small nuance: concurrent vs. parallel connections

A `ThreadPoolExecutor` with 4 workers means you’ll have at most 4 HTTP connections open at the same time to the DeepSeek API. That’s perfectly safe. Even 8 is very conservative. Many SDKs (including LiteLLM) use connection pooling under the hood, and the API can handle far more.

---

## What to do if you do hit a limit

If you ever see a `429` status code, LiteLLM has built‑in retry logic with exponential backoff. You can configure it via:

```python
lm = LiteLLM(
    model="deepseek/deepseek-chat",
    num_retries=3,               # retry on rate‑limit
    request_timeout=120,         # seconds
    ...
)
```

But for your use case, it’s unlikely you’ll need it.

---

## Bottom line

With a threadpool of 4–8 workers calling `deepseek-chat` (the non‑reasoning model you switched to), you are **far** from any concurrency or rate limit that would cause problems. Go ahead and run your calls in parallel — you’ll get your answers much faster and still stay comfortably within the API’s limits.

------------
**No, `deepseek-chat` does not have a `reasoning_effort` parameter.**  
That setting (`reasoning_effort`) is exclusive to the **reasoning models** (like `deepseek-reasoner` / R1). On `deepseek-chat`, there is no “effort” knob – the model either just answers directly, or, if you enable its **optional thinking mode**, it will generate a chain‑of‑thought but you can’t control how *hard* it thinks.

Here’s the distinction:

| Model               | Parameter to control reasoning                             | What you can do                                                               |
| ------------------- | ---------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `deepseek-chat`     | `thinking` (only `type: "disabled"` or `"enabled"`)        | Enable/disable an optional thinking step. **No effort levels.**               |
| `deepseek-reasoner` | `reasoning_effort` (values: `"low"`, `"medium"`, `"high"`) | Control how much reasoning the model does (it **always** reasons by default). |

So on `deepseek-chat`:

- If you **omit** the `thinking` parameter, or set `"type": "disabled"`, the model gives a direct answer (no chain‑of‑thought, no `reasoning_content`).  
- If you **enable** thinking with `"type": "enabled"`, the model will produce a hidden reasoning step before the answer, but you can’t dial the depth up or down – it’s a binary on/off switch.

If you want more elaborate reasoning, you have two options:

1. **Prompt engineering** – simply ask the model to “think step by step” or provide a detailed analysis. It will still produce only `content` (no separate `reasoning_content`) and the tokens are just part of the normal answer.
2. **Use a reasoning model** (`deepseek-reasoner`) with `reasoning_effort` set to `"low"`, `"medium"`, or `"high"` – then you’ll get a chain‑of‑thought whose length you can tune.

In summary: **deepseek-chat has no “effort” control**; the closest you can get is enabling or disabling the optional thinking mode, which is binary. For variable reasoning depth, you need `deepseek-reasoner`.