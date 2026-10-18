 `2026-08-06


If i do code changes for the celex service, I have to rebuild the image and restart the compose, correct? Is there a vscode container feature which support a auto rebuild/deploy/restart?


--->
**`docker compose watch`**.

Version 2.29.7 supports it. `docker compose watch` runs in the foreground and, on file change, applies a per-path action: `sync` (copy files into the running container), `rebuild` (rebuild the image and recreate), or `sync+restart`.

For this service I'd wire it as:

```yaml
    develop:
      watch:
        - action: sync
          path: ./src
          target: /app/src
        - action: sync
          path: ./config_main
          target: /app/config_main
        - action: rebuild
          path: ./requirements.txt
```

Python won't notice synced files on its own, so it pairs with `uvicorn --reload` — the sync drops the new file in, uvicorn restarts the app in-process, no image build and no container recreate. `requirements.txt` still triggers a real rebuild, which is right.

The honest caveat: **your edit loop stays ~40-50s regardless.** Reloading re-imports [[vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/ui/backend_server/oi_dspy.py|oi_dspy.py]], which loads the bge cross-encoder and builds all four DSPy modules at import time. Watch saves you the `pip install` layer check and the image export (a minute or two), not the app's cold start. Nothing short of moving that provisioning behind a lazy initializer changes that.

A `develop:` block only activates under `docker compose watch`, so normal `up -d` keeps using the baked-in code — no risk of a dev bind-mount leaking into a regular run.

On the VS Code side there's no equivalent auto-deploy feature. The Container Tools extension gives you the container tree, logs, attach and shell, and you can bind `docker compose watch` to a `tasks.json` task with `"isBackground": true` so it runs in the terminal panel — but the watching itself is Compose's. (Dev Containers is a different thing: it moves your _editor_ into a container for language services, rather than redeploying your service.)

