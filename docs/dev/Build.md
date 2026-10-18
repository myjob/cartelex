 `2026-08-31

In python, i have a vast code base but only parts should go to certain Docker container. how to organize the build, reduce the image to the mini but keep the project under a single umbrella?

-->

Since you're already on uv and a single repo, the cleanest fit is a **uv workspace**: one lockfile at the root, several small distributions underneath, and one Dockerfile whose target you select per service.

## Layout

```
celex/
├── pyproject.toml          # [tool.uv.workspace] members = ["packages/*"]
├── uv.lock                 # single lock → same versions everywhere
├── Dockerfile
└── packages/
    ├── celex-core/         # schema, pydantic models, db access, provenance SQL
    ├── celex-ingest/       # chunking, embedding calls, GCS
    ├── celex-temporal/     # HeidelTime wrapper — the only thing needing a JRE
    └── celex-api/          # DSPy LegalQA + FastAPI server for Open WebUI
```

Each member gets its own `pyproject.toml`, and depends on siblings via `celex-core = { workspace = true }`. The split should follow **dependency weight, not tidiness** — the point is that `celex-api` never has to pull in Java, HeidelTime, or anything ingestion-only. In your case the single biggest win is that the QA server talks to Ollama over HTTP, so its image needs no torch, no sentence-transformers, nothing CUDA. That alone is the difference between ~1.2 GB and ~250 MB.

## One Dockerfile, one build arg

```dockerfile
# syntax=docker/dockerfile:1.9
FROM python:3.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

FROM base AS builder
ARG PKG
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "$PKG"

FROM python:3.13-slim AS runtime
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH
RUN useradd -r -u 1001 app
USER app
```

The two flags doing the work:

- `--package celex-api` resolves from the shared lock but installs _only_ that member and its transitive deps.
- `--no-editable` installs workspace members as real wheels into `.venv`, so the runtime stage copies `/app/.venv` and nothing else. No source tree, no `.git`, no tests, no uv binary in the final image.

Per-service tails go in thin stages on top:

```dockerfile
FROM runtime AS api
CMD ["uvicorn", "celex_api.server:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS temporal
USER root
RUN apt-get update && apt-get install -y --no-install-recommends default-jre-headless \
 && rm -rf /var/lib/apt/lists/*
USER app
CMD ["celex-temporal", "worker"]
```

## Wiring it up

```yaml
services:
  qa-api:
    build:
      context: .
      target: api
      args: { PKG: celex-api }
    extra_hosts: ["host.docker.internal:host-gateway"]   # Ollama on the Spark
```

`docker buildx bake` with a matrix over `PKG` builds all of them in one go, and because every image shares the `base` layer they dedupe in the registry.

## `.dockerignore` as an allowlist

Since `COPY . .` in the builder pulls everything, invert it — otherwise `pg-data/`, `backup/`, and `.venv` end up in the build context and blow up your cache invalidation:

```
*
!pyproject.toml
!uv.lock
!packages/
packages/**/tests/
```

## If splitting into packages is too much churn right now

Interim step that gets most of the size benefit: keep one `pyproject.toml`, define `[project.optional-dependencies]` groups (`api`, `ingest`, `temporal`), install with `uv sync --extra api`, and `COPY src/celex/api src/celex/core` selectively instead of the whole tree. It's less clean — `deptry` can't enforce the boundaries for you, and nothing stops an import from crossing them — but it's a two-hour change rather than a two-day one, and it migrates naturally into the workspace layout later.