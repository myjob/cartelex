

Let me analyze the full scope of Agno references across the codebase.

Searched for "agno"
Searched for "from agno"
Listed directory agno-ui
Ran command: `find . -type d -name "*agno*" -o -type d -name "*agent_usr*" 2>/dev/null`
Listed directory agents_usr
Listed directory tools
Viewed time_event_tool.py:1-52
Viewed MarkdownHeaderChunking.py:1-64
Viewed fact_ingestor.py:1-151
Viewed timeline_table.py:1-263
Viewed event_processor.py:1-111
Viewed celex_checker.py:1-36
Viewed celex_tools.py:1-603
Viewed test_ollama_concurrency_requests.py:1-199
Viewed pyproject.toml:1-62

Now let me check for any imports of the `agents_usr` modules from elsewhere in the codebase:

Searched for "from agents_usr"
Searched for "import agents_usr"
Searched for "CelexDatabaseToolkit\|CelexCheckerToolkit\|TimeEventToolkit"
Searched for "GenericFactIngestor\|TimelineFactIngestor\|CartelPeriodFactIngestor"
Searched for "EventProcessor"
Searched for "MarkdownHeaderChunking"

Now let me check if `oi_dspy.py` has any Agno references (it's the file that will survive in `agno-ui/`):

Searched for "agno"
Listed directory tests

I now have a complete picture. Let me create the analysis artifact.

Created agno_removal_plan.md

The full analysis is in the [agno_removal_plan.md](file://~/.gemini/antigravity/brain/a0147de9-41c2-453c-845b-3e80ad9ab031/artifacts/agno_removal_plan.md) artifact.

**Key findings:**

- **`agents_usr/` (9 agents + 13 tests) and `oi-play.py`** → safe to delete outright, zero external consumers outside themselves
- **6 pipeline files** still have Agno imports (`celex_tools.py`, `celex_checker.py`, `time_event_tool.py`, `event_processor.py`, `fact_ingestor.py`, `timeline_table.py`, `MarkdownHeaderChunking.py`) — most appear dead/obsolete but need your confirmation
- **8 doc files** have prose references (cleanup last)
- **`pyproject.toml`** → remove `agno==2.3.21` after code is clean

**Before I proceed, I need your answers on 4 decision points** (detailed in the plan's Phase 2):

1. Is `CelexDatabaseToolkit` still used at runtime, or only via `oi-play.py`?
2. Is `EventProcessor` still active, or fully superseded?
3. Are `TimelineFactIngestor` / `GenericFactIngestor` still called, or replaced by `PipelineFactIngestor`?
4. Keep `test_ollama_concurrency_requests.py` (rewrite) or delete?