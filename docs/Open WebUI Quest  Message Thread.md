 `2026-08-31
Open WebUI is almost certainly already sending the whole history — it posts the full `messages` array to `/v1/chat/completions` on every turn. So the context is arriving; your DSPy server is throwing it away. Two things to check, then the actual fix.

## First, rule out the connection type

If the CELEX server is registered as a **Tool Server** rather than under Connections → OpenAI API, it only ever receives extracted arguments, never the conversation. That alone would produce exactly your symptom. Verify it's an OpenAI-compatible model provider.

Then log what you actually receive:

```python
@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    log.info("roles=%s", [m["role"] for m in body["messages"]])
```

If you see `['user', 'assistant', 'user']` on turn two, the history is there and the bug is `question = messages[-1]["content"]` somewhere in your handler.

## The fix has two halves

Naively concatenating history into `LegalQA(question=...)` won't work for your pipeline, because "list those cases" is useless as a retrieval query — its embedding has nothing to do with cartels, and `GenerateKBSQL` has no referent for "those".

**Half one: condense the follow-up into a standalone question** before it touches retrieval.

```python
class CondenseFollowup(dspy.Signature):
    """Rewrite the latest user turn as a standalone question.
    Resolve anaphora ("those cases", "that company", "them") against the
    conversation. Do not answer. If already standalone, return unchanged."""
    history: dspy.History = dspy.InputField()
    question: str = dspy.InputField()
    standalone_question: str = dspy.OutputField()
```

`dspy.History` is the built-in multi-turn field type — you pass `dspy.History(messages=[{"question": ..., "answer": ...}, ...])`. If your DSPy version predates it, just render the turns into a plain string field; the module is small enough that it doesn't matter much.

**Half two — the one that matters more for your pipeline: carry the resolved result set, not just the text.** "List those cases" refers to a set of `celex_id`s that turn one already computed. Re-deriving them from a rewritten question is lossy and non-deterministic. Pin them instead:

```python
class TurnState(BaseModel):
    standalone_question: str
    sql: str | None = None
    celex_ids: list[str] = []
    company_ids: list[int] = []
```

Then let your router classify the incoming turn as `new_query` vs `refine_previous`. On `refine_previous`, skip retrieval entirely and query against the pinned IDs (`WHERE celex_id = ANY(:prev_ids)`). This also keeps the provenance chain intact — the follow-up answer traces back to the same `fact_source_link` rows, which matters given your audit-grade requirement. A rewritten-question path would silently return a _different_ set of cases and you'd never notice.

## Keying the state

Open WebUI 0.10.0+ lets you attach templated custom headers on the connection config. Add these in Admin Settings → Connections:

```json
{
  "X-OpenWebUI-Chat-Id": "{{CHAT_ID}}",
  "X-OpenWebUI-Message-Id": "{{MESSAGE_ID}}",
  "X-OpenWebUI-Task": "{{TASK}}"
}
```

Now key your state store on `X-OpenWebUI-Chat-Id`. Reconcile against edits and regenerations by truncating to the turn index:

```python
turn_idx = sum(1 for m in messages if m["role"] == "user") - 1
states = STORE.setdefault(chat_id, [])[:turn_idx]
```

That header is also how you filter out noise: Open WebUI fires background calls to your endpoint for **title generation, tag generation, and follow-up suggestions**. Those arrive as ordinary completions with strange prompts and will corrupt your state store and burn DGX Spark inference time. Check `X-OpenWebUI-Task` and short-circuit anything that isn't a real user turn.

Two settings worth turning off while you debug, both under Admin → Interface: title auto-generation, and "Query Generation" for retrieval — the latter rewrites queries upstream of you and will fight your `CondenseFollowup` module.