I have a big python process with grown and refactored over time. some libraries in .venv are outdated or not even used any longer. my former effort to update requirements.txt or pyproject.toml sometimes caused missing dependency.

provide a step-to-step guide how to cleans without breaking
-->

Here's a safe, incremental approach. The core problem with your past attempts is almost certainly this: pip/pyproject.toml only track **direct** dependencies, but your code (or a dependency's code) may be importing something that's only present as a **transitive** dependency of a package you later removed. You need tooling that distinguishes "declared but unused" from "used but undeclared" — doing this by eye is how you get silent breakage.

## Step 0 — Safety net (don't skip)

```bash
# Freeze exactly what currently works, as ground truth to roll back to
pip freeze > requirements-known-good.txt
git add -A && git commit -m "checkpoint before dependency cleanup"

# Snapshot the venv itself so you can restore instantly if needed
cp -r .venv .venv-backup   # or just keep requirements-known-good.txt and recreate
```

If you don't already have a test suite you trust as a safety check, this is the moment to make sure `pytest` runs green _before_ touching anything — you mentioned 700+ tests already, so that's your regression harness for every step below.

## Step 1 — Find what's actually imported vs. installed

Use **deptry** — it's built exactly for this problem (unused deps _and_ transitive-only deps that should be direct):

```bash
pip install deptry
deptry .
```

It reports three categories you care about:

- `DEP002` unused dependencies (declared, never imported)
- `DEP001` missing dependencies (imported, not declared — these are your silent-breakage risks)
- `DEP003` transitive dependencies used directly without being declared

Also get a dependency tree so you can see what's a leaf vs. what's load-bearing for other packages:

```bash
pip install pipdeptree
pipdeptree --warn silent > tree-before.txt
pipdeptree --reverse -p <package>   # "what depends on this?" — check before removing anything
```

## Step 2 — Reconcile import names vs. package names

A common false-positive source: import name ≠ PyPI name (`PIL`→`pillow`, `yaml`→`pyyaml`, `cv2`→`opencv-python`). deptry handles most of these via its mapping, but double check anything DSPy/pgvector/HeidelTime-adjacent — smaller or niche packages are more likely to trip this up.

## Step 3 — Remove unused packages one at a time, not in bulk

This is the key discipline change from your past attempts. Batch removal makes failures unattributable.

```bash
for pkg in pkgA pkgB pkgC; do
  pip uninstall -y "$pkg"
  pytest -q || { echo "BROKE ON $pkg"; break; }
done
```

Commit after each successful removal (or small batch of obviously-safe ones like linters/dead notebook libs). If something breaks, `git bisect`-style attribution becomes trivial because each commit isolates one change.

## Step 4 — Rebuild the lockfile from a clean resolve, don't hand-edit

Hand-editing `pyproject.toml`/`requirements.txt` is what caused your past breakage — you were guessing at the dependency graph instead of letting a resolver compute it. Two solid options:

**If you're open to switching tooling** (recommended, and it's fast — worth it given your DGX/local-infra comfort):

```bash
pip install uv
uv init --no-workspace   # or point at existing pyproject.toml
uv add $(deptry . 2>&1 | grep DEP001 | awk '{print $NF}')  # add anything deptry flagged as missing
uv lock
uv sync
```

`uv.lock` pins the _entire resolved graph_, transitive deps included — that's what prevents the "missing dependency" surprise, because nothing gets installed that isn't explicitly resolved and recorded.

**If you want to stay on pip/pip-tools:**

```bash
pip install pip-tools
pip-compile pyproject.toml -o requirements.lock.txt
pip-sync requirements.lock.txt
```

## Step 5 — Validate in total isolation

Don't trust the venv you've been mutating — it may have stale artifacts. Build fresh from the new lockfile and run the full suite there:

```bash
python -m venv .venv-clean
source .venv-clean/bin/activate
pip install -r requirements.lock.txt   # or uv sync
pytest -q
```

If green, swap it in for `.venv`. If not, deptry + pipdeptree output tells you exactly which import is unresolved — much faster to diagnose than a bare `ModuleNotFoundError` at runtime deep in a DSPy pipeline.

## Step 6 — Guard against regression going forward

Add deptry as a lint step (pre-commit or CI) so unused/missing deps get caught immediately rather than accumulating again:

```yaml
# .pre-commit-config.yaml
- repo: local
  hooks:
    - id: deptry
      name: deptry
      entry: deptry .
      language: system
      pass_filenames: false
```

---

One thing specific to your setup worth flagging: DSPy, pgvector bindings, and HeidelTime/TIMEX3 wrappers sometimes pull in optional or platform-specific transitive deps (e.g., Java bridges for HeidelTime) that static import analysis won't catch because the "import" happens via subprocess or dynamic loading rather than a Python `import` statement. Deptry won't flag these as used — so before removing anything in that neighborhood, check it manually or grep for subprocess/`importlib`/dynamic invocation before trusting the "unused" verdict.