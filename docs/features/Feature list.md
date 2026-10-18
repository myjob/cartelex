# epics
#corp_index #corp_index_translate #corp_detail_microservice
#entity_graph
#knowledge_qa
#fact2book_verify
#ui
#claw_send_concert_emails
#rclone_GDrive

# features
- [x] fact extractor language of doc chunks
- [ ] #fact2book_veriyfeature 20260629: case_in_book_landmark # F Economic landmark cases
- [ ] entire: add claude sessions to github
- [ ] feature 20260706: knowledge graph ### Phase 5 — New knowledge representations (fixes W8, W9; Part 2 items) 1. **Entity graph*
- [x] split UI test case from backend and  add switch to scripts/run_test. if the backend server oi_dpsy isnt running stop tests graceful.
- [ ] should the java process created by HeidelTime be killed after usage?
- [ ] #ui looping QA agent: complex question such as: for brewery case, check if any corp applied for leniency outside of U.K
- [ ] #fact2book_veriyfeature compare celex facts with book facts ! in progress
- [x] purge table events_extraction & events_extraction_attributes and its refs
- [ ] purge the views  timeline%
- [x] feature 20260624: auto login to sparky with key
- [ ] feature 20260624: read celex docs from Google cloud storage rather than local
- [ ] feature 20260617: does celex_facts.knowledge_items need embeddings?
- [x] feature 20260623: extend oi_dspy with an get API to render MD and PDF in the open webui as seperate tab
- [x] My retrieval module in DSPy with open web UI delivers good results by searching my RAG, extracting pre-ingested facts and knowledge items. How could i build on top a retriever who combines sub retrievals such as: collect all legal cases affecting banks & provide their period of cartels as a histogram over years [compositional retrieval](compositional retrieval)
- [ ] #corp_index feature 20260706: add open corp data; account with Niki
- [ ] #corp_index feature 20260605: The corp index with loads from https://www.opensanctions.org/datasets/gb_coh_psc/ a.o could build a microservice for any EU corp retrieval
- [ ] feature 20260617:  run src/batch/Main_ingest_knowledge.py every now and then (cron triggered?) or event based such as every 10 new kb queue
- [ ] feature 20260617:  DB mig alembic https://alembic.sqlalchemy.org/en/latest/tutorial.html
- [x] feature 20260531: rename  table celex_fact_ingestion_audit_trail to fact_ingestion_audit_trail
- [x] feature 20260531: celex_fact_ingestion_audit_trail has pipeline as attribute which would be nice for source register fact link as well
- [x] feature 20260530: add data lineage of facts with  [Data Lineage Option & Implementation](../Data%20Lineage%20Option%20&%20Implementation.md)

- [x]  feature 20260529: in open web ui, I want to present the sources of a certain response
      To add an icon as a suffix to every response in Open WebUI, you have three distinct paths depending on whether you want a UI action button, an automatic text emoji/icon suffix, or a graphical image/SVG link.

Here is exactly how to achieve each method:

## Method 1: Create a Custom "Action" Button (Cleanest UI Look)


If you want an actual interactive or visual icon button to natively appear directly underneath the model's message box alongside the native copy/thumbs-up icons, use an Action Function. [1]

1. Go to Workspace > Functions and click + Add Function.
2. Set the type to Action.
3. Paste the following boilerplate code. It appends your custom icon button to the footer of the response: [2]

```python
"""
title: Custom Suffix Icon
author: Assistant
version: 1.0
icon_url: https://iconify.design
"""
from pydantic import BaseModel, Field
import json

class Action:
    async def action(self, body: dict, __event_emitter__=None) -> bytes:
        # Define what happens when the icon is clicked (optional)
        if __event_emitter__:
            await __event_emitter__({
                "type": "status",
                "data": {"description": "Icon clicked!", "done": True}
            })
        return b""
```

1. Save it and toggle it On. Go to Workspace > Models, edit your model, and enable this Action under the model settings. [3, 4]

## Method 2: System Prompt Injection (For Text Emojis or Icons)

If you simply want an emoji icon (like 🚀, ✨, 🤖) printed as a suffix at the absolute end of the message string text, adjust the system instructions.

1. Navigate to Workspace > Models and click the ✏️ Edit icon next to your default model.
2. Scroll to the System Prompt field.
3. Append this rule to the very bottom of your prompt:
    
    > _"CRITICAL: You must always conclude your final sentence with exactly one space followed by the icon '[YOUR_EMOJI_HERE]'. Do not add any text or newlines after this icon."_
    
4. Click Save. [3, 5]

## Method 3: Custom "Filter" Function (For Hardcoding Markdown Icons/Images)

If you want a Markdown-rendered image icon or an HTML-supported visual automatically appended to the text string programmatically (without relying on the model remembering a prompt), use a Filter Function.

1. Go to Workspace > Functions > + Add Function.
2. Select Filter as the function type.
3. Use the `outgoing` hook to intercept the response text and append an image link or HTML snippet right before rendering: [6]

```python
"""
title: Automatic Response Suffix Icon
author: Assistant
version: 1.0
"""
from typing import Optional

class Filter:
    async def outgoing(self, body: dict) -> dict:
        # Check if this is an assistant message
        messages = body.get("messages", [])
        if messages and messages[-1].get("role") == "assistant":
            # Append a Markdown image/icon link or a standard icon code
            suffix = " ![icon](https://iconify.design)"
            messages[-1]["content"] = messages[-1]["content"].rstrip() + suffix
            
        return body
```

1. Save it, enable the toggle, and link it to your active model inside Workspace > Models. [3, 4]

If you would like to go with the Filter or Action Function, let me know:

- What specific icon you want to display (e.g., a checkmark, warning sign, custom logo)
- Should the icon be clickable to trigger an action, or is it purely decorative?

  

[1] [https://www.youtube.com](https://www.youtube.com/watch?v=K3J3GCKJBN0&t=5)

[2] [https://docs.openwebui.com](https://docs.openwebui.com/features/extensibility/plugin/functions/action/)

[3] [https://docs.openwebui.com](https://docs.openwebui.com/features/extensibility/plugin/tools/)

[4] [https://www.reddit.com](https://www.reddit.com/r/OpenWebUI/)

[5] [https://www.youtube.com](https://www.youtube.com/watch?v=Fd_1zePgCLE)

[6] [https://www.reddit.com](https://www.reddit.com/r/OpenWebUI/comments/1n291ag/how_do_you_make_openwebui_render_an_image/)