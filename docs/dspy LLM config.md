```

    def _configure_llm(self) -> None:
        """Configure the global DSPy LM against the Ollama OpenAI-compatible API."""
        lm = dspy.LM(
            f"openai/{settings.LLM_MODEL_ID}",
            api_base=f"{settings.OLLAMA_HOST}/v1",
            api_key="ollama",
            cache=False,  # No benefit for unique legal case inputs
            # 1. Steer DSPy-level Retries (how many times DSPy will retry its own wrapper)
            num_retries=1,
            # 2. Steer OpenAI/LiteLLM Client Retries (fixes the `openai._base_client` log)
            # Set to 0 if you want it to fail fast instead of doing exponential backoff
            max_retries=0,
            # 3. Steer Request Duration / Timeout (in seconds)
            timeout=240.0,
        )
        dspy.configure(lm=lm)
```

see: src/batch/Main_ingest_generic_facts.py  class FactIngestionOrchestrator


**Why caching helps (default `cache=True`)** DSPy caches LM responses to disk so that identical inputs return instantly without re-calling the API. It's useful when you're iterating on prompts and re-running the same examples repeatedly during development.

**Why it doesn't help you — and may hurt**

- **Legal cases are unique** — each case has distinct facts, parties, and context, so the chance of an exact cache hit across hundreds of different inputs is effectively zero.
- **Cache writes are wasted I/O** — DSPy still writes every response to the cache store (typically a SQLite file via `diskcache`), burning disk I/O and growing the cache file for entries you'll never read again.
- **Memory pressure** — the cache can grow large with hundreds of long legal documents, potentially causing slowdowns.
- **No repeated runs** — caching pays off when you re-run the _same_ inputs. In a one-shot batch pipeline, that never happens.