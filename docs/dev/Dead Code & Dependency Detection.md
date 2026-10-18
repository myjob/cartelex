
see step-by-step guide in [dependency-modernization](../infra/Runbook%20—%20Python%20Framework%20Dependency%20modernization%20step-by-step%20guide.md)

For Python, the go-to toolchain is **Vulture** (for detection) combined with **deadcode** (for a stricter second opinion and optional auto-removal). Here's a practical breakdown:

---

## The tools

### 1. Vulture — primary detector

Vulture is the best-known tool for detecting globally unused Python code, as opposed to linters like Ruff which only catch locally unused things.

```bash
pip install vulture
vulture myproject/
```

Vulture uses the `ast` module to build abstract syntax trees for all given files and records the names of defined and used objects. It assigns each chunk of dead code a confidence value — 100% means the code will never be executed; values below 100% are estimates.

**Key flags for safe operation:**

- `--min-confidence 100` reports only code that is _guaranteed_ to be unused within the analyzed files — the safest starting point.
- `--sort-by-size` — prioritize the biggest wins first.
- `--make-whitelist` — auto-generates a whitelist for things that look dead but are actually used dynamically.

### 2. deadcode — stricter second opinion

`deadcode` implements more rules than Vulture, tracks scopes and namespaces more accurately to reduce false positives, and provides a `--fix` option to automatically remove detected unused code items.

```bash
pip install deadcode
deadcode myproject/          # detect
deadcode myproject/ --fix    # auto-remove (use carefully!)
```

---

## The safe workflow

Since you want to be **absolutely sure** you break nothing, follow this sequence:

**Step 1 — Start conservative:** run Vulture at 100% confidence

```bash
vulture myproject/ --min-confidence 100
```

**Step 2 — Cross-check with ripgrep** before deleting anything

```bash
rg "MyUnusedClass" myproject/ tests/
```

**Step 3 — Run your test suite with coverage** to catch what static analysis misses

```bash
pytest --cov=myproject --cov-report=html
```

Coverage-based detection is complementary to static analysis — it detects code that's never executed during tests, finding different issues.

**Step 4 — Handle false positives with a whitelist**, not by ignoring entire files:

```bash
vulture myproject/ --make-whitelist > whitelist.py
vulture myproject/ whitelist.py
```

The whitelist approach is recommended because whitelists are automatically checked for syntactic correctness and you can even pass them to your Python interpreter to verify that all whitelisted code actually still exists in your project.

**Step 5 — Commit dead code removal separately** from other changes, with a clear message describing what was removed and why.

---

## Important caveat for your codebase

Framework awareness matters — if your dead code tool doesn't understand Python frameworks (DSPy signatures, Pydantic models, dynamic dispatch patterns), most of its output is noise. Given your pipeline uses DSPy, pgvector, and HeidelTime, you'll likely have false positives from dynamically referenced classes and `__init_subclass__`-style patterns. The whitelist is your friend there — build it up iteratively rather than trying to clean everything in one pass.


is there no tool which can deal with DSPy and pydantic? shouldnt be rocket science
-->
Yes, there is — and it's exactly the gap that motivated someone to build **Skylos**. The short version: no tool natively understands DSPy (it's too niche), but the situation with Pydantic is well covered.

**Skylos** is a framework-aware static analysis tool that explicitly understands Pydantic, Django, Flask, FastAPI, SQLAlchemy, and other frameworks — reducing false positives compared to Vulture.
https://github.com/duriantaco/skylos

The numbers are pretty stark. On the FastAPI repo alone (which is heavily Pydantic), Vulture reported 102 false positives while Skylos reported 30. On the Pydantic repo itself, Vulture produced 112 false positives vs Skylos's 93. Specifically on the Pydantic false positive problem:

Pydantic `BaseModel` fields define your API schema — they're serialized and validated at runtime, but Vulture sees them as unused variables. Skylos knows not to flag them.

On a FastAPI + Pydantic repo seeded with 29 known dead-code items, Skylos found all 29 while Vulture found 24 — with the tradeoff being Skylos takes ~1.6s for deeper AST context versus Vulture's ~0.1s.

```bash
pip install skylos
skylos .
```

**The DSPy caveat:** DSPy is obscure enough that no tool has built-in awareness of it. The patterns that'll generate false positives are:

- `dspy.Signature` subclasses with `InputField`/`OutputField` class attributes — these look like unused class-level variables
- `dspy.Module` subclasses where `forward()` is called dynamically
- Teleprompter/optimizer callbacks

For those you'd still need a Vulture whitelist, but the volume of noise will be dramatically lower with Skylos handling the Pydantic side. You can also add `# noqa` or `# type: ignore`-style inline suppressions for known DSPy patterns, or just maintain a small `whitelist_dspy.py` covering your signature field patterns — a few lines rather than hundreds.


## How many dependency spec files do I need?
you need **two**, not three. Here's what each actually does in this repo:

|File|Role|Verdict|
|---|---|---|
|`pyproject.toml`|Authored by you. Declares _intent_ — the ~30 packages you actually import, as loose ranges. Also holds packaging metadata and the ruff/pyright/skylos config.|**Keep**|
|`requirements.txt`|Generated. The _lock_ — all ~190 packages including transitives, pinned exactly, so a Docker rebuild in six months produces the same tree.|**Keep, but generate it**|
|`uv.lock`|A second lock. Same job as `requirements.txt`, different tool.|**Delete**|

The split exists because those are two different questions: "what does this project need" versus "what exact tree did we resolve that to". You can't get both from one file — pin exactly in `pyproject.toml` and your entire transitive tree floats free (torch, the nvidia CUDA stack, transformers all resolving to whatever is newest on build day). That's precisely the drift you have now.

`uv.lock` is pure redundancy. I grepped the Dockerfile, docker-compose.yml, and scripts/ — **nothing references it**. It's an abandoned experiment from April. Wave 5 of the plan already deletes it; given you're asking, I'd move that to Wave 1 so there's never a moment with two competing locks.

The oi_dspy backend doesn't use uv at all — [Dockerfile:57-58](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/Dockerfile-open-celex-facts-backend-oi_dspy#L57-L58) is plain `pip install --no-cache-dir -r requirements.txt`. There is no `uv` binary in the image, no `uv sync`, no reference to `uv.lock` anywhere in the Dockerfile, docker-compose.yml, or scripts/. The lock file is inert.

If you're asking whether to _switch_ it to uv, there are two shapes, and both still need a lock — just a different one:

**Option A — uv as a faster pip (minimal change)**

```dockerfile
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY requirements.txt ./requirements.txt
RUN uv pip install --system --no-cache -r requirements.txt
```

`requirements.txt` stays the lock. `uv.lock` still gets deleted. You get the resolver speed — a meaningful win on this dependency tree, where torch plus the CUDA wheels dominate build time — with no change to the source-of-truth story. `uv pip compile` can also replace `pip-compile` in Wave 1, same output format.

**Option B — full uv (`uv sync`)**

`uv.lock` becomes the lock and `requirements.txt` is deleted. But this fights the current image design: `uv sync` installs the _project_ as a package, while your container deliberately doesn't — it sets `PYTHONPATH=/app/src` and copies `src/` in as plain files. You'd need `uv sync --frozen --no-install-project`, and it only works once `pyproject.toml` parses at all.

## My read

Option A. The reproducibility that matters lives in the pinned lock, and `requirements.txt` already fills that role in the one place it counts — the image build. Swapping the lock format buys you nothing here and costs you a Dockerfile rewrite while the manifest is still broken.

Either way the answer to "do I need three files" is unchanged: one authored manifest, one generated lock, and `uv.lock` goes. Whether uv is the tool that _reads_ that lock is a separate, lower-stakes decision you can make after Wave 3.

Want me to add Option A as an optional step in Wave 5, and move the `uv.lock` deletion up to Wave 1? Tell me which copy of the plan is canonical now — the `docs/claude/plans/` original or your `docs/infra/` copy — so I don't edit the wrong one.

## One thing worth knowing

The Docker image never installs the package. [Dockerfile:57-58](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/Dockerfile-open-celex-facts-backend-oi_dspy#L57-L58) does only `pip install -r requirements.txt`, then [line 80](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/Dockerfile-open-celex-facts-backend-oi_dspy#L80) sets `PYTHONPATH=/app/src` and copies `src/` in. So in production, `requirements.txt` is the _only_ manifest that matters — `pyproject.toml`'s dependency list is never consulted.

That's also the source of your `pip check` errors. Your local venv has an editable install (`__editable__.celex_facts-0.1.0.pth`), and its `dist-info` metadata was baked from an _older_ pyproject — which is why pip still thinks the project requires `agno`. The stale metadata, not the current file, is what's complaining.

That doesn't make `pyproject.toml` optional. It's the input `pip-compile` reads to produce `requirements.txt`, and without it you're back to hand-editing 190 pins.

Want me to fold this into the plan — move the `uv.lock` deletion up to Wave 1 and add a note that the container bypasses `pyproject.toml`? You'll need to re-copy to `docs/infra/`, or tell me which of the two copies is now canonical so I don't leave them diverging.