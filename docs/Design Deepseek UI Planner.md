# Design: DeepSeek LLM switch for the oi_dspy UI backend

## Purpose

`src/ui/backend_server/oi_dspy.py` is the FastAPI/DSPy backend behind the
Open WebUI chat interface. It previously hardcoded a local Ollama model as
its DSPy LM. This document describes the `UI_LLM_PROVIDER` config switch
that lets it run against DeepSeek's cloud API instead, with no code changes.

## Configuration

`src/config/settings.py`:

```python
LLM_MODEL_CLOUD_ID = os.getenv("LLM_MODEL_CLOUD_ID", "deepseek-v4-flash")
LLM_MODEL_CLOUD_API_KEY = os.environ["DEEPSEEK_API_KEY"]

# --- UI LLM Provider Toggle ---
# Selects the LM backing the oi_dspy UI backend server.
# "ollama" = local LLM_MODEL_ID via OLLAMA_HOST (default),
# "deepseek" = cloud LLM_MODEL_CLOUD_ID via DEEPSEEK_API_KEY.
UI_LLM_PROVIDER = os.getenv("UI_LLM_PROVIDER", "ollama")  # "ollama" | "deepseek"
```

`DEEPSEEK_API_KEY` is read from `config_main/.env`. `LLM_MODEL_CLOUD_ID`
defaults to `deepseek-v4-flash`; a commented-out line in settings.py shows
the alternative `deepseek-v4-pro`.

## Dispatch

`src/ui/backend_server/oi_dspy.py` selects the LM at startup based on
`settings.UI_LLM_PROVIDER`:

```python
if settings.UI_LLM_PROVIDER == "deepseek":
    print(f"Configuring DSPy LM (deepseek: {settings.LLM_MODEL_CLOUD_ID}) …")
    lm = create_deepseek_lm()
else:
    print(f"Configuring DSPy LM (ollama: {settings.LLM_MODEL_ID}) …")
    lm = dspy.LM(
        f"openai/{settings.LLM_MODEL_ID}",
        api_base=f"{settings.OLLAMA_HOST}/v1",
        api_key="ollama",
        cache=True,
    )
dspy.configure(lm=lm)
```

Any value other than `"deepseek"` falls back to the Ollama path, so an
unset or misspelled env var preserves current default behaviour.

`create_deepseek_lm()` is not new code written for this switch — it already
existed in `src/pipelines/corp_index/corp_index_module.py` for the
corporate-identifier lookup module, and is reused here via
`pipelines.corp_index.__init__`'s `__all__` export. It builds a
LiteLLM-compatible `dspy.LM` from settings:

```python
def create_deepseek_lm() -> dspy.LM:
    return dspy.LM(
        _cloud_model_name(settings.LLM_MODEL_CLOUD_ID),  # -> "deepseek/deepseek-v4-flash"
        api_key=settings.LLM_MODEL_CLOUD_API_KEY,
        cache=True,
    )
```

## How to switch

Default (Ollama, unchanged):

```
PYTHONPATH=src .venv/bin/uvicorn ui.backend_server.oi_dspy:app --host 0.0.0.0 --port 7777
```

DeepSeek:

```
UI_LLM_PROVIDER=deepseek PYTHONPATH=src .venv/bin/uvicorn ui.backend_server.oi_dspy:app --host 0.0.0.0 --port 7777
```

Or set `UI_LLM_PROVIDER=deepseek` in `config_main/.env` to make it the
persistent default for that environment.

## Verification performed

- `ruff check` passed on both modified files.
- Started the server with default settings: startup log printed
  `Configuring DSPy LM (ollama: qwen3-coder:30b)`; `GET /models` returned 200.
- Started the server with `UI_LLM_PROVIDER=deepseek`: startup log printed
  `Configuring DSPy LM (deepseek: deepseek-v4-flash)`; `GET /models` returned
  200; a real `POST /v1/chat/completions` question ("What was the fine in
  case CELEX-32002D0759?") streamed a full pipeline run (validate → plan →
  retrieve → synthesize) using the DeepSeek-backed LM.

## Gotcha: where env vars actually come from

Resolved 2026-07-29. There is now exactly one env file, `config_main/.env`
(`chmod 600`, gitignored), loaded in-process by `src/config/settings.py` via
`load_dotenv(ENV_FILE)` where `ENV_FILE = PROJECT_ROOT / "config_main" /
".env"`. Because the load happens at import of `config.settings`, every runner
sees it identically: terminal, VS Code test runner and debugger, cron, and
systemd. The earlier repo-root `.env` and `src/config/.env` are both gone, and
the duplicate `export DEEPSEEK_API_KEY` in `~/.bashrc` has been removed.

One rule still applies: `load_dotenv` does **not** override an already-set
process variable, so a stale `export` in a shell silently beats the file. Keep
one owner per variable.

The historical incident that motivated this: `UI_LLM_PROVIDER=deepseek` had at some point only
been exported in an interactive shell (never written to either `.env` file),
so every fresh server start silently fell back to the `"ollama"` default in
settings.py even though `DEEPSEEK_API_KEY` was present and correct. Symptom:
`oi_dspy`'s DEBUG logs kept showing `POST http://localhost:11434/...`
instead of DeepSeek, with no error — the switch code was correct, the
environment just didn't have the variable set where it mattered.

Fix applied at the time: added `UI_LLM_PROVIDER=deepseek` to `src/config/.env`,
then restarted the uvicorn process so it picked up the new value. Confirmed via
the startup log line (`Configuring DSPy LM (deepseek: ...)`), `GET /models`
returning 200, and a live chat request producing zero hits to the Ollama host
in the log.

**That setting is no longer persisted.** `UI_LLM_PROVIDER` was not present in
`src/config/.env` when its contents were migrated to `config_main/.env`, so the
UI backend falls back to the `"ollama"` default on every fresh start. Re-add
`UI_LLM_PROVIDER=deepseek` to `config_main/.env` if the DeepSeek-backed UI is
wanted as the default.

**Takeaway:** if `UI_LLM_PROVIDER` (or any settings.py env var) doesn't seem
to take effect, check `config_main/.env` — and check that nothing is exporting
the same variable from a shell, which would override it.

## Known issue found during verification

The DeepSeek-backed synthesis answered "the evidence does not contain any
information regarding the fine" for the CELEX-32002D0759 test question, even
though the attached provenance chunk (doc_pos 42, "FINAL AMOUNT OF THE
FINES") plainly lists the fine amounts. Retrieval fetched the right chunk;
the synthesizer failed to ground its answer in it. Tracked in
[ISSUES.md](../ISSUES.md) for investigation (possibly the `fines` fact value
reaching the synthesizer, not the chunk text, is empty/truncated — compare
against the Ollama path).

## Related files

- `src/config/settings.py` — `UI_LLM_PROVIDER`, `LLM_MODEL_CLOUD_ID`,
  `LLM_MODEL_CLOUD_API_KEY`, `LLM_MODEL_ID`, `OLLAMA_HOST`.
- `src/ui/backend_server/oi_dspy.py` — provider dispatch at startup.
- `src/pipelines/corp_index/corp_index_module.py` — `create_deepseek_lm()`,
  `_cloud_model_name()`.
- `docs/claude/plans/ui-llm-provider-switch-ollama-deepseek.md` — original
  implementation plan.
- `ISSUES.md` — open items: DeepSeek synthesis-grounding miss, and batch
  scripts duplicating the inline Ollama `dspy.LM(...)` block that a shared
  factory could unify with this toggle.

---
[src/config/.env:3](vscode-webview://0lsanv6vomkuohfig8ok8m62bcnhf9u8fulb482g3rvpe6lbchtm/src/config/.env#L3) now reads `UI_LLM_PROVIDER=ollama`.

Notes:

- This is the only `.env` `settings.py` loads, and it wins over the `os.getenv` default. With it set to `ollama`, `oi_dspy` will use the local Ollama model (`settings.LLM_MODEL_ID` = `qwen3-coder:30b` via `OLLAMA_HOST`).
- The `settings.py` default was already `"ollama"`, so no code change was needed — just this env override.
- **Restart the uvicorn `oi_dspy` server** for the change to take effect — the LM is selected once at startup, not per request.

I left `DEEPSEEK_API_KEY` and `LLM_MODEL_CLOUD_ID` untouched so you can flip back to `deepseek` any time.