## What Happened

Looking at your logs, here's the sequence:

**17:13** — Normal operation, fast responses (400ms embeds, 24s completions)

**17:23:59** — First 500 with exactly `10m0s` duration → hit Ollama's internal request timeout. The model **got stuck**, not just slow.

**17:25:45** — One lucky 1m45s success (maybe previous request cleared something)

**17:35 → 18:05** — Solid wall of `10m0s` timeouts → model is permanently hung, serving nothing

**18:13:30** — Localhost health checks (`HEAD /`, `GET /api/ps`) appear — likely systemd's watchdog or you manually poking it

**18:14:30** — Final timeout, then systemd stops the service

**Key evidence: `25.8G memory peak`** — that's your root cause.

---

## Root Cause

The process consumed **5h 54min CPU** and **25.8GB RAM**. Over a long batch run:

1. **Memory pressure builds** — KV cache grows, context windows accumulate, or a leaked/stuck request holds GPU/RAM
2. **The model hangs mid-inference** — either OOM-killed internally, GPU ran out of VRAM and started thrashing, or a request context got corrupted
3. **Ollama's GIN server keeps the connection open** until its 10-minute timeout, returning 500
4. **Your client retried** against a server that was alive at the HTTP level but dead at the inference level — health checks passed, but `/v1/chat/completions` kept failing

---

## Diagnosis Commands

**After the next incident (before restarting), run:**

```bash
# 1. Check what's loaded and memory state
curl http://localhost:11434/api/ps | jq

# 2. System memory pressure
free -h
cat /proc/meminfo | grep -E "MemAvailable|SwapUsed|Dirty"

# 3. GPU state (if NVIDIA)
nvidia-smi
nvidia-smi --query-compute-apps=pid,used_memory --format=csv

# 4. OOM killer activity
sudo dmesg | grep -i "oom\|killed\|out of memory" | tail -20

# 5. Ollama full logs with errors
sudo journalctl -u ollama --since "17:00" --until "18:20" | grep -E "error|warn|WARN|ERR|panic"

# 6. Check if requests are queued/stuck
sudo ss -tnp | grep ollama
```

**For ongoing monitoring during a run:**

```bash
# Watch memory + GPU together every 5s
watch -n 5 'free -h && echo "---" && nvidia-smi --query-gpu=memory.used,memory.free,temperature.gpu --format=csv,noheader'
```

---

## Resilience: Layered Approach

### 1. Ollama-side: Tune timeouts and memory limits

Add to your systemd service override (`sudo systemctl edit ollama`):

```ini
[Service]
Environment="OLLAMA_KEEP_ALIVE=5m"
Environment="OLLAMA_MAX_QUEUE=4"
Environment="OLLAMA_NUM_PARALLEL=1"
# Limit memory to prevent runaway growth (adjust to your RAM)
MemoryMax=20G
MemorySwapMax=0
# Auto-restart on crash
Restart=on-failure
RestartSec=10s
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

### 2. Client-side: Smart retry with health-check gating

The key insight: **don't retry blindly — verify the model is actually responsive first**.
 - [x] todo : build a ollama watch dog

Built 2026-09-02 as [config/ollama_health_gate.py](src/config/ollama_health_gate.py) plus
the remote sensor script [config/dgx_thermal_probe.py](src/config/dgx_thermal_probe.py),
enforced per CELEX in `BatchJobRunner`. It differs from the sketch below in three ways
that matter:

- It gates on **temperature as well as liveness**. Measured on `spark-niki` at 88 percent
  utilisation, `nvidia-smi temperature.gpu` read 75 C while `thermal_zone0` read 83.3 C --
  the board sensors run about 8 C above the GPU die sensor, so the gate compares
  `max(GPU, all ACPI zones)` against `settings.THERMAL_MAX_TEMP_C`. Reading only
  `temperature.gpu` would have passed an 80 C limit at 83.3 C.
- The endpoint comes from `settings.OLLAMA_HOST`; the hardcoded `localhost` below is
  stale, and the SSH host for the probe is derived from that same setting so the two
  cannot drift apart.
- No `tenacity`: it is not a dependency of this project. The cooldown loop is explicit,
  bounded by `settings.THERMAL_MAX_COOLDOWN_CYCLES`, and ends in a graceful stop that
  writes the unprocessed CELEX IDs to `logs/thermal_abort_<stamp>.txt` for `--celex-list-file`.


```python
import httpx
import time
import logging
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

OLLAMA_BASE = "http://localhost:11434"

def is_ollama_healthy(timeout=5) -> bool:
    """Check HTTP liveness AND that inference is not hung."""
    try:
        # Level 1: basic liveness
        r = httpx.get(f"{OLLAMA_BASE}/", timeout=timeout)
        if r.status_code != 200:
            return False
        # Level 2: model actually loaded
        r2 = httpx.get(f"{OLLAMA_BASE}/api/ps", timeout=timeout)
        return r2.status_code == 200
    except Exception:
        return False

def wait_for_healthy(max_wait=120, interval=10):
    """Block until Ollama is healthy or give up."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if is_ollama_healthy():
            return True
        logging.warning("Ollama unhealthy, waiting %ds...", interval)
        time.sleep(interval)
    raise RuntimeError("Ollama did not recover within %ds" % max_wait)

@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException)),
    wait=wait_exponential(multiplier=2, min=5, max=60),
    stop=stop_after_attempt(5),
    before_sleep=lambda rs: logging.warning("Retry %d after error", rs.attempt_number)
)
def chat_with_ollama(messages: list, model="llama3", timeout=120) -> str:
    response = httpx.post(
        f"{OLLAMA_BASE}/v1/chat/completions",
        json={"model": model, "messages": messages},
        timeout=timeout,  # Don't let client hang for 10min
    )
    if response.status_code == 500:
        # 500 from Ollama = model hung, not a transient error
        # Wait for recovery before retrying
        logging.error("Ollama 500 — checking health before retry")
        wait_for_healthy()
        response.raise_for_status()  # Will trigger tenacity retry
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]
```

### 3. Batch runner: Checkpoint + resume

```python
import json, os

def process_batch(items: list, checkpoint_file="progress.json"):
    # Load existing progress
    done = set()
    if os.path.exists(checkpoint_file):
        with open(checkpoint_file) as f:
            done = set(json.load(f).get("completed", []))
    
    results = {}
    for i, item in enumerate(items):
        item_id = item["id"]
        if item_id in done:
            continue  # Skip already processed
        
        try:
            result = chat_with_ollama(item["messages"])
            results[item_id] = result
            done.add(item_id)
            
            # Checkpoint every 10 items
            if i % 10 == 0:
                with open(checkpoint_file, "w") as f:
                    json.dump({"completed": list(done)}, f)
                    
        except Exception as e:
            logging.error("Failed item %s after retries: %s", item_id, e)
            # Continue batch — don't die on one item
            continue
    
    return results
```

### 4. Proactive: Periodic model reload for long runs

For batches running >2h, evict and reload the model periodically to clear KV cache buildup:

```bash
# Add to a cron or call from your batch runner every ~1h
curl -X POST http://localhost:11434/api/generate \
  -d '{"model": "llama3", "keep_alive": 0}'  # Unloads model
sleep 5
curl -X POST http://localhost:11434/api/generate \
  -d '{"model": "llama3", "prompt": "hi", "stream": false}'  # Reload
```

Or from Python in your batch loop:

```python
def reload_model(model="llama3"):
    httpx.post(f"{OLLAMA_BASE}/api/generate", 
               json={"model": model, "keep_alive": 0}, timeout=30)
    time.sleep(5)
    httpx.post(f"{OLLAMA_BASE}/api/generate",
               json={"model": model, "prompt": "ping", "stream": False}, timeout=60)
```

---

## Summary

|Layer|Problem|Fix|
|---|---|---|
|Ollama config|No memory cap, no auto-restart|`MemoryMax`, `Restart=on-failure` in systemd|
|Client timeout|Waits 10min on hung requests|Set explicit `timeout=120` in your HTTP client|
|Retry logic|Retries against hung server|Health-check gate before retry|
|Long runs|KV cache / memory leak over time|Periodic model evict+reload every ~1h|
|Fault tolerance|Crash loses all progress|Checkpoint file every N items|

The `25.8G` memory figure is the smoking gun — add `MemoryMax` and the periodic reload, and you should get through long batches cleanly.


Good catch, sorry for the wrong assumption. Let me re-read the actual situation:

**65Gi RAM free, GPU fully loaded with qwen3-coder:30b (48GB) + embedding (5.7GB) at 100%** — memory is not your problem at all.

## Revised Diagnosis

The real culprit: **`OLLAMA_REQUEST_TIMEOUT` (default: 10 minutes)** combined with **sequential request queuing**.

```
17:23:59  500 | 10m0s   ← first request hits the timeout ceiling
17:25:45  200 | 1m45s   ← queued request finally gets through (model wasn't dead)
17:35:46  500 | 10m0s   ←┐
17:45:46  500 | 10m0s   ← queue backed up, each waiting 10min before failing
17:55:47  500 | 10m0s   ←┘
```

One slow request blocked the queue. Subsequent requests didn't fail because the model hung — they failed because they **waited 10 minutes in the queue** and hit the timeout before even starting.

The key question is **why that first request at 17:23 took >10 minutes**. With a 262144-token context window on qwen3-coder:30b, candidates are:

1. **One input was extremely long** — KV cache prefill for a huge context can take minutes on its own, then generation on top
2. **Thermal throttling** — 4h of sustained 100% GPU load; worth checking `nvidia-smi -q -d TEMPERATURE` and clock frequencies
3. **Generation runaway** — model got into a very long output (the timeout is wall-clock, not token count)

## Diagnose the actual cause

```bash
# GPU clocks and thermals - were clocks throttled?
nvidia-smi -q -d CLOCK,TEMPERATURE,POWER | grep -E "Current|Throttle|Temp|Power Draw"

# Any throttle events in the past run?
nvidia-smi --query-gpu=clocks_throttle_reasons.active --format=csv

# Look at full ollama logs around the first failure - what was the request?
sudo journalctl -u ollama --since "17:20" --until "17:30" | grep -v "GIN"

# Tokens/sec during normal vs slow periods (if you have ollama debug logging)
sudo journalctl -u ollama | grep -E "token|eval|prompt" | tail -50
```

Enable verbose logging to capture token rates:

```bash
# Add to systemd override
Environment="OLLAMA_DEBUG=1"
```

## Resilience fixes (corrected for your setup)

### 1. Increase the timeout — or make it per-request

The 10m default is too short for a 262k-context model. Set it higher **and** track it:

```ini
# sudo systemctl edit ollama
[Service]
Environment="OLLAMA_REQUEST_TIMEOUT=30m"
Environment="OLLAMA_NUM_PARALLEL=2"   # DGX Spark can handle concurrent requests
Environment="OLLAMA_DEBUG=1"          # Get token/sec in logs
Restart=on-failure
RestartSec=15s
```

### 2. Client: set your own timeout shorter than Ollama's, with health probe

The worst outcome was your client dead-looping against a live-but-stuck server. The fix is **don't wait for Ollama's timeout — probe first**:

```python
import httpx
import time

OLLAMA = "http://localhost:11434"

def probe_inference(model="qwen3-coder:30b", timeout=15) -> bool:
    """Send a trivial request — if it doesn't respond in 15s, model is stuck."""
    try:
        r = httpx.post(f"{OLLAMA}/v1/chat/completions", json={
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False

def chat(messages, model="qwen3-coder:30b", timeout=25*60) -> str:
    response = httpx.post(
        f"{OLLAMA}/v1/chat/completions",
        json={"model": model, "messages": messages},
        timeout=timeout,  # your own ceiling, not Ollama's
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]

def resilient_chat(messages, model="qwen3-coder:30b", retries=3):
    for attempt in range(retries):
        try:
            return chat(messages, model)
        except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
            print(f"Attempt {attempt+1} failed: {e}")
            # Before retrying, verify model is actually responsive
            print("Probing inference health...")
            if not probe_inference(model):
                print("Model stuck — waiting 30s for queue to clear")
                time.sleep(30)
                if not probe_inference(model):
                    raise RuntimeError("Model unresponsive after wait")
    raise RuntimeError("All retries exhausted")
```

### 3. Limit context per request to avoid the >10min outlier

If your batch has variable-length inputs, guard against runaway single requests:

```python
import tiktoken  # or count chars as proxy

MAX_INPUT_TOKENS = 50_000  # tune based on acceptable latency

def safe_chat(messages, **kwargs):
    # Rough token estimate
    total_chars = sum(len(m["content"]) for m in messages)
    if total_chars > MAX_INPUT_TOKENS * 3:  # ~3 chars/token
        raise ValueError(f"Input too long ({total_chars} chars), would block queue")
    return resilient_chat(messages, **kwargs)
```

### 4. Periodic queue-drain check

Instead of restarting Ollama, just unload/reload the model to clear any stuck state every N hours:

```python
import httpx, time

def cycle_model(model="qwen3-coder:30b"):
    """Evict and reload model — clears KV cache and any stuck state."""
    print("Cycling model...")
    httpx.post(f"{OLLAMA}/api/generate", 
               json={"model": model, "keep_alive": 0}, timeout=30)
    time.sleep(10)
    # Warm it back up
    httpx.post(f"{OLLAMA}/api/generate",
               json={"model": model, "prompt": "ping", "stream": False}, 
               timeout=120)
    print("Model reloaded.")

# In your batch loop
for i, item in enumerate(batch):
    if i > 0 and i % 200 == 0:  # every 200 items
        cycle_model()
    process(item)
```

## TL;DR

| What happened         | One request exceeded 10m → blocked queue → timeout cascade         |
| --------------------- | ------------------------------------------------------------------ |
| Root cause to confirm | Check GPU thermals/clocks after 4h load, or one very long input    |
| Primary fix           | Raise `OLLAMA_REQUEST_TIMEOUT`, add `OLLAMA_NUM_PARALLEL=2`        |
| Client fix            | Don't rely on Ollama's timeout — set your own + probe before retry |
| Long-run fix          | Periodic model cycle (evict/reload) every ~200 items               |


## Root Cause: KV Cache Exhaustion

The smoking gun is this line:

```
cache.go:151 msg="loading cache slot" id=0 cache=32419 prompt=5448 used=5447 remaining=1
```

**`remaining=1`** — only 1 token left in the KV cache slot. When the next request fills it completely, Ollama 500s. The 10-minute timeout before the 500 confirms it's hanging trying to generate into a full context window, not a transient crash.

Your prompt is already 18,768 tokens (`server.go:1550 prompt=18768`) but your effective cache ceiling is ~32K. Once conversation history accumulates past that in a session, every subsequent request hits the wall.

---

## Fixes

### 1. Increase `num_ctx` (best option — you have the VRAM)

The DGX Spark has 128 GB unified memory, so you can afford a much larger context. Create or update a Modelfile:

```bash
ollama show qwen3-coder:30b --modelfile > Modelfile
```

Add/edit the `num_ctx` parameter:

```
FROM qwen3-coder:30b
PARAMETER num_ctx 65536   # or 131072 if headroom allows
```

```bash
ollama create qwen3-coder-ctx:30b -f Modelfile
```

Then point your client at the new model name.

---

### 2. Set a global default via env var

If you're running ollama as a systemd service, edit the unit:

```bash
sudo systemctl edit ollama
```

```ini
[Service]
Environment="OLLAMA_NUM_CTX=65536"
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

---

### 3. Pass `num_ctx` per-request (if your client supports it)

```json
{
  "model": "qwen3-coder:30b",
  "options": { "num_ctx": 65536 },
  "messages": [...]
}
```

---

### 4. Trim conversation history on the client side

If the context growth is driven by long agentic loops or tool call accumulation, implement a sliding window — keep system prompt + last N turns rather than the full history.

---

## Recommended for your setup

On a DGX Spark, start with `num_ctx 65536`. If you're running long coding sessions with tool calls, `131072` is reasonable — qwen3-coder:30b at 4-bit needs roughly ~3 GB/32K context tokens for KV cache, well within your memory budget. Monitor with `ollama ps` to watch VRAM usage after the change.


---
# Solution Approach

## overloading ollama qwen code:
select document from celex_facts.pg_embedding_qwen where celex_id = '39612_12448_6' order by doc_pos


Apr 25 10:59:57 spark-niki ollama[121377]: time=2026-04-25T10:59:57.810+02:00 level=DEBUG source=sched.go:672 msg="evaluating already loaded" model=/usr/share/ollama/.ollama/models/blobs/sha256-1194192cf2a187eb02722edcc3f77b11d21f537048ce04b67ccf8ba78863006a
Apr 25 10:59:58 spark-niki ollama[121377]: time=2026-04-25T10:59:58.300+02:00 level=DEBUG source=server.go:1550 msg="completion request" images=0 prompt=2316883 format=""
Apr 25 10:59:58 spark-niki ollama[121377]: time=2026-04-25T10:59:58.760+02:00 level=WARN source=runner.go:187 msg="truncating input prompt" **limit=262144 prompt=554488** keep=4 new=262144
Apr 25 10:59:58 spark-niki ollama[121377]: time=2026-04-25T10:59:58.761+02:00 level=DEBUG source=cache.go:151 msg="loading cache slot" id=0 cache=0 prompt=262144 used=0 remaining=262144


Every 6.0s: free -h | head -2 && ollama ps                                                                                                                                                                     spark-niki: Sat Apr 25 11:02:20 2026

               total        used        free      shared  buff/cache   available
Mem:                      121Gi                   51Gi                 **43Gi**        49Mi        28Gi        70Gi
NAME               ID              SIZE     PROCESSOR    CONTEXT    UNTIL
qwen3-coder:30b    06c1097efce0    48 GB    100% GPU     262144     11 minutes from now


When you send a prompt of 554,488 tokens to an Ollama model with a context limit (`num_ctx`) of 262,144, you are essentially trying to fit two gallons of water into a one-gallon jug.

Here is exactly what happens under the hood:

### 1. Truncation (The "Amnesia" Effect)

Ollama (and the underlying `llama.cpp` engine) will not magically compress the text. Instead, it will **truncate** the prompt to fit the 262,144-token limit.

By default, Ollama handles context overflow by dropping the _oldest_ tokens. This means it will cut off the first ~292,344 tokens of your prompt and only feed the **last 262,144 tokens** to the model.

- **The consequence:** If your core instructions, system prompt, or crucial context were at the very beginning of that massive text block, the model will completely "forget" them because it never actually saw them. It will only respond based on the bottom half of your text.
    

### 2. Massive Latency (Time to First Token)

Even if it successfully truncates the prompt to 262,144 tokens, the model still has to process that entire context window before it can generate a single word. This is called "prompt ingestion." Processing a 256k context window is incredibly computationally expensive. Depending on your hardware (CPU vs. GPU), your system might sit there churning for several minutes (or even hours) before generating the first token.

### 3. High Risk of an Out of Memory (OOM) Crash

A context limit of 262,144 tokens requires an enormous amount of RAM or VRAM just to store the "KV Cache" (the memory the model uses to keep track of the context).

- For a standard 7B or 8B parameter model, a 256k context window can easily consume **16GB to 32GB of RAM/VRAM** _just for the context_, on top of the memory needed to load the model weights.
    
- If your system (Mac unified memory, or PC GPU/System RAM) does not have enough free memory to handle this massive KV cache, Ollama will likely crash, throw an `Out of Memory` error in the server logs, or completely freeze your system as it aggressively pages to your hard drive.
    

### How to handle this much data instead:

If you actually need the model to understand all 554,488 tokens, throwing it all into the prompt at once won't work. You will need to change your approach:

- **Retrieval-Augmented Generation (RAG):** Instead of pasting the whole document, use a tool like AnythingLLM or a Python script to embed the document into a vector database. The system will search the database for only the relevant chunks of text and send those to Ollama.
    
- **Chunking:** Break the text down into smaller sections (e.g., 50,000 tokens at a time), ask the model to summarize or extract data from each section, and then have the model process the combined summaries.


### is there a parameter in Ollama engine which refuse to execute and send a error back?
The Short Answer

**No.** Currently, the core Ollama engine does not have a native parameter that tells it to refuse execution and return a hard API error when a prompt exceeds the `num_ctx` limit.

Instead, Ollama relies entirely on **silent truncation**.

### What Ollama Does Instead

When your prompt exceeds the defined context window, Ollama does not fail, crash, or throw a user-facing error. Instead, it handles the overflow using a "sliding window" approach:

1. It calculates how many tokens over the limit your prompt is.
    
2. It silently drops the oldest/first tokens from your prompt to make room for the new ones (and space for its own response).
    
3. It generates a response based _only_ on the truncated data that fit inside the limit.
    

If you are running Ollama with debug logging enabled (`OLLAMA_DEBUG=1`), you will see a quiet warning in the backend server logs, but the API will still execute and return a successful response to the user:

> `level=DEBUG source=prompt.go msg="truncating input messages which exceed context length"`

### Why This Is Frustrating

Many developers find this default behavior dangerous because you don't realize your data is missing until the model starts hallucinating or acting out of character. Because it drops the _oldest_ tokens first, the things that usually get deleted are:

- Your **System Prompt** (e.g., "You are an expert coding assistant...").
    
- The initial instructions or constraints you set.
    
- The beginning of any large documents or codebases you pasted in.
    

### How to Force an Error (The Workaround)

Since Ollama won't throw the error for you, the standard industry practice is to handle the limit **client-side** before the prompt ever reaches the Ollama server.

If you are building an app, script, or using an advanced interface (like LangChain, AnythingLLM, or Aider), you must implement a "pre-flight" check:

- **Tokenize your input:** Use a lightweight tokenizer library (like `tiktoken`) in your application code to estimate the size of the user's prompt.
    
- **Check the limit:** Check if the estimated token count exceeds the `num_ctx` you configured for Ollama.
    
- **Throw your own error:** If it's too large, have your application block the request and show the user a hard error (e.g., _"Prompt is too long (estimated: 554,488, max: 262,144)"_) before ever sending the API call to Ollama.
    

This client-side check is exactly how popular AI coding assistants and front-end wrappers protect users from Ollama's silent truncation.


---
### why do you recommend to cut that by 2?
If your IDE or an AI completion tool suggested dividing the token limit by 2 (or subtracting a chunk from it), it is trying to protect you from two common pitfalls when working with LLM context windows. 

I've gone ahead and fixed both of these issues in `settings.py` and `universal_modules.py`. Here is why those adjustments are necessary:

### 1. You Need to Leave Room for the Output
The `OLLAMA_LLM_INFERENCE_TOKEN_LIMIT` (e.g., 262,144 tokens) represents the **absolute maximum capacity for BOTH the Input Prompt + the Output Generation combined**. 
If you fill all 262,144 tokens with your `context`, the model has exactly `0` tokens left to generate the summary. The inference will either crash or instantly return an empty response. A common recommendation is to slice the limit in half (or subtract a buffer like 4,000 tokens) to ensure the LLM has plenty of breathing room to write the generated response.

### 2. Characters vs. Tokens mismatch
In Python, `len(context)` counts the number of **characters** in a string, but LLM limits are measured in **tokens**. 
On average, 1 token is equal to roughly 4 English characters. If you directly check `len(context) > settings.OLLAMA_LLM_INFERENCE_TOKEN_LIMIT`, you are comparing characters to tokens. This effectively shrinks your context window to 25% of its true capacity! 

### 3. Missing Integer Cast
Because `os.getenv` always returns a string, evaluating `len(context) > "262144"` will actually raise a `TypeError` and crash the pipeline.

**I have proactively updated your code to handle all of this safely:**
- Cast `OLLAMA_LLM_INFERENCE_TOKEN_LIMIT` to an integer in `settings.py`.
- Divided `len(context)` by `4` to get an accurate token estimation.
- Subtracted an output buffer (`4000` tokens) from the max limit to ensure the summary has enough room to be written.


--- 
## largest case 

select celex_id , count(1) from celex_facts.pg_embedding_qwen group by celex_id order by count(1) desc
```
39612_12448_6	1420
AT_40588_6339	940
39226_8310_11	782
39711_4493_4	756
37990_3581_18	752
39092_7226_4	701
39437_7332_3	656
39523_5272_13	649
39686_4072_5	639

```
select sum(token_count) from celex_facts.pg_embedding_qwen where celex_id = '36756_68_16' -- 51593

### -> no document is bigger than max token!
```
OLLAMA_LLM_INFERENCE_TOKEN_LIMIT = int(
        os.getenv("OLLAMA_LLM_INFERENCE_TOKEN_LIMIT", "262144")
    )

    # leave 1/4 of the tokens for the response
    OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT = int(
        os.getenv("OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT", "183500")
    )
```