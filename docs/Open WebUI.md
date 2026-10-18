


#quests

# Open WebUI does not render HTML response
Short version: Open WebUI doesn't render arbitrary raw HTML in chat output. It runs the assistant text through a Markdown pipeline (marked) and then sanitizes the result with DOMPurify. So two things are happening to your string.

The big tell is that your `# 1. Subject of the proceeding` is _also_ showing literally. If the Markdown renderer were running over this block, `#` would become an `<h1>`. Since even the `#` is plain text, this block is not being parsed as Markdown at all — it's being treated as a **code block**, which is exactly why your `<strong>` tags appear as literal characters too. Everything-literal almost always means "this landed inside `<pre><code>`."

The two usual causes:

- **The content is wrapped in a code fence.** If your model/pipeline emitted ` ```html ... ``` ` (very common when a model "returns HTML"), Open WebUI renders the whole thing verbatim. Strip the fences before sending.
- **Leading indentation.** Four or more leading spaces on lines makes marked treat them as an indented code block. If your HTML string is pretty-printed/indented, the renderer reads it as code. Left-align it.

Once it's actually going through Markdown, the second issue kicks in: DOMPurify strips structural/unknown stuff. Inline tags like `<strong>`, `<em>`, `<a>` survive, but `<div class slot>` and similar layout/attribute HTML get sanitized out, so don't rely on them.

So the practical fix depends on what you control:

The cleanest path is to **stop sending HTML and send Markdown**, since that's the native format. `# 1. Subject of the proceeding` already is a heading; replace `<strong>X</strong>` with `**X**` and drop the wrapper `<div>`. It'll render correctly and survive sanitization.

If you genuinely need HTML (e.g. something Markdown can't express), make sure the string has no code fences and no 4-space indentation, keep block-level tags separated by blank lines, and stick to tags/attributes DOMPurify allows — structural divs with `class`/`slot` won't render.

Which is it for you — are you emitting this from a model response, a pipe/function, or a tool/custom frontend? That changes where you'd strip the fences or swap to Markdown. Also worth confirming: is the literal text appearing inside a grey code-block box in the UI, or as bare unstyled text? That single detail tells us code-block vs. escaped-HTML for certain.

That `&lt;` confirms it: the tags aren't being rendered _or_ treated as a code block — they're being **HTML-escaped**. Open WebUI took the literal characters `<pre>` and `<strong>` and converted them to `&lt;pre&gt;` / `&lt;strong&gt;` before inserting them into the DOM, so the browser paints them as visible text. That's marked/DOMPurify escaping raw HTML it doesn't trust.

And the root of it is the `<pre>` wrapper. Your model output is literally:

```
<pre># 1. Subject...<strong>...</strong>...</pre>
```

Two problems stacked on top of each other:

1. **It's wrapped in `<pre>`.** Even if the HTML weren't escaped, `<pre>` is preformatted text — Markdown inside it never renders, so your `#` would stay a literal hash regardless.
2. **The whole thing is raw HTML**, which Open WebUI escapes rather than rendering.

The fix is to stop emitting HTML tags entirely and emit **Markdown**, which is what the renderer actually wants:

```
# 1. Subject of the proceeding

(1) This proceeding concerns an agreement between the **Vereeniging ter Bevordering van de Belangen des Boekhandels** (...) ...
```

That gives you the `<h1>` and the bold rendered properly, and nothing gets escaped because there's no HTML to sanitize.

The `<pre>` wrapper is almost certainly your model adding it on its own (LLMs love wrapping "a document" in `<pre>` or a code fence). So the real lever is upstream: either tell the model in its system prompt to respond in plain Markdown with no `<pre>`/HTML wrappers, or strip a leading `<pre>`/trailing `</pre>` (and convert `<strong>`→`**`) in your pipeline before the text reaches Open WebUI.

Where is this text generated — is it a direct model response you can steer with a system prompt, or is it coming out of a pipe/function where you'd post-process it? If it's a prompt, one line like "Output GitHub-flavored Markdown only; never wrap the answer in `<pre>`, code fences, or HTML tags" usually kills it outright.
# Conversation thread handling (20260831)

Open WebUI posts the **whole** `messages` array to `/v1/chat/completions` on
every turn. The backend used to read only `messages[-1]` (plus a sticky celex
lookup), so a dependent follow-up reached the planner with no referent — "list
those cases" after a cross-case question planned an unscoped `kb_sql` and
returned 348 unrelated cases.

`src/pipelines/agent_dspy/conversation_context.py` now supplies two halves:

- **Condense** — a pure `looks_dependent()` pre-gate decides whether the turn
  needs the thread at all. Only a dependent turn reaches
  `CondenseFollowupSignature`, which rewrites it into a standalone question or
  classifies it `standalone` (history dropped). A self-contained question costs
  zero extra inference.
- **Pin** — the case set a turn resolved is written into that turn's answer and
  read back on the next one, so a refinement re-queries exactly the cases the
  user was shown. No server-side store: nothing keyed on a chat id, nothing lost
  on restart, no cross-request races.

The pin marker is a **markdown link-reference definition**:

```
[celex-context]: #CELEX-31994D0823,CELEX-31969D0216
```

A definition nothing links to renders as nothing under CommonMark, and carries
no HTML for DOMPurify to escape into visible text (see the sanitizing notes
above). Sanitizing is display-only — the stored message is the raw stream, so
the marker returns on the next turn regardless.

Open WebUI's background calls (title, tag and follow-up generation) hit the same
endpoint. They are now rejected by `is_meta_task_prompt()` before the pipeline
runs, instead of each costing a full plan-execute-synthesize on the DGX.

## Switches

All in `src/config/settings.py`, env-overridable via `config_main/.env`
(gitignored, so this table is the shareable record). Each is read at the call
site; changing one needs a server restart.

| Flag | Default | Controls |
| --- | --- | --- |
| `CONVERSATION_HISTORY_ENABLED` | `true` | Master switch. Off restores the pre-20260831 behaviour exactly. |
| `CONVERSATION_CONDENSE_ENABLED` | `true` | The follow-up rewrite. Off keeps pinning but makes no condense call. |
| `CONVERSATION_CONDENSE_ALWAYS` | `false` | Bypass the `looks_dependent` pre-gate and condense every turn (A/B the heuristic; costs one LLM call per turn). |
| `CONVERSATION_PIN_CELEX_ENABLED` | `true` | Writing and reading the pinned case set. Off leaves condense-only behaviour: the question is rewritten, the cases re-derived. |
| `CONVERSATION_HISTORY_MAX_TURNS` | `6` | Prior turns shown to the condenser. |
| `CONVERSATION_PIN_MAX_CASES` | `60` | Cap on pinned ids, so a broad first answer cannot produce an unbounded `IN` list. |
| `WEBUI_REJECT_TASK_PROMPTS` | `true` | The meta-task gate. Off lets title/tag/follow-up calls run the pipeline again. |

Two Open WebUI settings worth turning off while debugging this, both under
Admin -> Interface: title auto-generation, and "Query Generation" for retrieval —
the latter rewrites queries upstream and fights the condenser.

Design and verification: [webui-message-thread-context-20260831](claude/plans/webui-message-thread-context-20260831.md).
