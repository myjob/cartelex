# Design: Document Viewer

> Implementation plan: [document-viewer-endpoints-oi-dspy.md](claude/plans/document-viewer-endpoints-oi-dspy.md)

## Purpose

Allow users of the Open WebUI chat interface to open a case's original source documents — the
PDF decision and the mineru-extracted Markdown — directly from the chat footer, addressed by
`celex_id`, in a separate browser tab.

## Endpoints

All endpoints are served by the backend uvicorn process (`oi_dspy.py`, default port 7777). They
are read-only `GET` routes; no authentication is required beyond network access.

| Route                     | Returns                                                                          |
| ------------------------- | -------------------------------------------------------------------------------- |
| `GET /doc/{celex_id}`     | Tabbed HTML viewer (Markdown / PDF) embedding the two leaf routes via `<iframe>` |
| `GET /doc/{celex_id}/md`  | mineru-extracted `.md` file rendered to a styled HTML page                       |
| `GET /doc/{celex_id}/pdf` | Original PDF streamed inline (`Content-Disposition: inline`)                     |

All three endpoints share the `_html_page(body, title)` helper shell for consistent styling. Each
leaf endpoint returns a **404 HTML page** when the file is absent, mirroring the existing
`/chunk/{chunk_id}` behaviour.

## On-disk layout

```
<CELEX_DOCS_DIR>/
  <celex_id>/
    <celex_id>.pdf
    mineru/
      auto/
        <celex_id>.md
```

`CELEX_DOCS_DIR` is read exclusively from `settings.CELEX_DOCS_DIR` (env-overridable via
`CELEX_DOCS_DIR`, default `~/a/celex_docs`). No hardcoded paths appear in the
implementation; redirecting the viewer to a different store is a config/env change only.

## Path safety

`celex_id` values are validated against `^[A-Za-z0-9_-]+$` before any filesystem access.
After regex validation, `_resolve_doc_path` also asserts `target.resolve().is_relative_to(_DOCS_ROOT)`
as defence-in-depth against encoded traversal attempts. A malformed or routable-but-invalid id
returns **400**; a URL with encoded traversal sequences (`..%2f..`) is normalised by the Starlette
router before it reaches the handler and returns **404** (no file is ever served).

## Chat footer integration

After every answered query, `stream_generator` appends a `_doc_footer(celex_id)` Markdown block
to the SSE stream. The footer embeds two icon links:

```
[📄 MD](<CHUNK_VIEW_BASE_URL>/doc/<celex_id>/md)  [📕 PDF](<CHUNK_VIEW_BASE_URL>/doc/<celex_id>/pdf)
```

`CHUNK_VIEW_BASE_URL` is the existing backend base URL setting (default `http://localhost:7777`).
The footer is a no-op (empty string) when no case was resolved for the current turn.

The footer is also appended on `case_context_set` events (when a user sets a CELEX with no
question), so the document links appear immediately upon context selection.

## Key implementation details

- **Markdown rendering** uses `markdown_it` (`MarkdownIt("commonmark").enable("table")`), which
  is already present in `.venv`. A module-level renderer instance is shared across requests.
- **Tabbed viewer** (`/doc/{celex_id}`) is a minimal inline-scripted HTML page: two `<button>`
  elements toggle `display` on two `<iframe>` elements; Markdown is shown by default.
- **`_html_page` shell** is shared by `/chunk/{chunk_id}`, `/doc/{celex_id}/md`, and the 404
  pages. It renders `body` HTML inside a minimal, responsive document with system-font typography,
  `pre`-wrap, and table styles.
- **No changes** to the chat pipeline, streaming, or existing routes.

## Settings added

| Setting | Default | Purpose |
|---|---|---|
| `CELEX_DOCS_DIR` | `~/a/celex_docs` | Filesystem root for case document directories |

`CHUNK_VIEW_BASE_URL` was already present; the footer links use it unchanged.

## Routing caveat

These endpoints are served by the backend uvicorn process, not by Open WebUI itself.
`/doc/*` links open correctly when the browser can reach the backend directly
(e.g. `http://<host>:7777/doc/<celex_id>/pdf`). If traffic flows through a reverse proxy, a
`/doc/*` forwarding rule must be added to route requests to the backend.

## Test coverage

`src/ui/backend_server/tests/test_doc_viewer.py` — 7 isolated unit tests (no DB, no LLM).
The docs root is patched via `mock.patch.object(oi_dspy, "_DOCS_ROOT", ...)` so tests write to
a `tempfile.TemporaryDirectory`.

Cases covered: valid PDF, valid Markdown, tabbed viewer, missing PDF (404), missing Markdown
(404), malformed `celex_id` (400), and encoded traversal (normalised to 404 by Starlette).
