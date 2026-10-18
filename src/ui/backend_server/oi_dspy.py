"""
OpenAI-compatible API server backed by the DSPy QARetrievalPipeline.

Usage (from project root, with .venv activated):
    PYTHONPATH=src .venv/bin/uvicorn ui.backend_server.oi_dspy:app --host 0.0.0.0 --port 7777
    PYTHONPATH=src uvicorn ui.backend_server.oi_dspy:app --host 0.0.0.0 --port 7777
    cd src/ui/backend_server &&
    PYTHONPATH=~/a/celex-facts/src uvicorn oi_dspy:app --host 0.0.0.0 --port 7777

Requires:
  - the ui_planner LLM profile selects the LM backend:
    settings.UI_LLM_PROVIDER is "ollama" (default, needs Ollama running) or
    "deepseek" (needs DEEPSEEK_API_KEY), and settings.UI_LLM_MODEL_ID names the
    model -- blank means that provider's shared default. Configured separately
    from the corp_index profile, so repointing the batch leaves the UI alone.
  - the ui reranker profile selects the in-process reranker:
    settings.UI_RERANKER_BACKEND is "flashrank" (default, the small ONNX
    model on CPU) or "bge" (the batch cross-encoder, RERANKER_MODEL_ID unless
    UI_RERANKER_MODEL_ID names another). Independent of RERANKER_BACKEND so
    a batch run's setting never re-points this server.
  - PostgreSQL with celex_facts schema populated
"""

import html
import json
import logging
import re
import time
from pathlib import Path

import dspy
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field
from sqlalchemy import text
from typing import Iterable, Iterator, List

from config.llm_factory import create_lm, ui_planner_profile
from config.settings import settings
from extract.rag.hybrid_search import HybridSearch
from extract.rag.ranker.ranker_factory import create_ranker, ui_ranker_profile
from extract.rag.enumeration_search import EnumerationSearch
from pipelines.agent_dspy.conversation_context import (
    encode_pinned_context,
    parse_history,
)
from pipelines.agent_dspy.enumeration_fact_module import EnumerationFactModule
from pipelines.agent_dspy.evidence_verifier_module import EvidenceVerifierModule
from pipelines.agent_dspy.knowledge_stage_filter import (
    apply_stage_filter,
    is_meta_task_prompt,
    normalize_prompt,
)
from pipelines.agent_dspy.knowledge_probe import KnowledgeProbe
from pipelines.agent_dspy.master_planner_module import MasterPlannerModule
from pipelines.agent_dspy.retrieval_modules import QARetrievalPipeline
from pipelines.agent_dspy.retrieval_router import RetrievalRouterModule
from pipelines.rag.pgvector.entity_graph_repository import EntityGraphRepository
from pipelines.rag.pgvector.fact_evidence_repository import FactEvidenceRepository
from pipelines.rag.pgvector.fact_repository import FactRepository
from pipelines.rag.pgvector.knowledge_repository import KnowledgeRepository
from pipelines.rag.pgvector.lineage_repository import LineageRepository
from pipelines.rag.pgvector.models import KnowledgeExtractionQueue
from pipelines.rag.pgvector.session import Session as PgSession
from tools.celex_tools import CelexDatabaseToolkit

# No ``logging.basicConfig`` here: importing ``config.settings`` above already
# applied ``config_main/logging_config.yaml`` through ``dictConfig``, so a
# module-scope call was a silent no-op (root already owns handlers) that made
# every test importing this module look like it reconfigured the root logger.
# Console verbosity belongs to that YAML, for the server and the suite alike.
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CrossCaseAggregator — concrete implementation (same as integration tests)
# ---------------------------------------------------------------------------


class FactAggregator:
    """Read-only SQL execution over celex_facts.facts.

    Satisfies the ``CrossCaseAggregator`` protocol.
    """

    def __init__(self, engine) -> None:
        self.engine = engine

    def aggregate_facts(self, sql: str) -> list[dict]:
        with self.engine.connect() as conn:
            result = conn.execute(text(sql))
            columns = list(result.keys())
            return [dict(zip(columns, row)) for row in result.fetchall()]


# ---------------------------------------------------------------------------
# Provision all dependencies at startup
# ---------------------------------------------------------------------------

ui_profile = ui_planner_profile()
logger.debug(
    f"Configuring DSPy LM ({ui_profile.provider}: {ui_profile.model_id})"
)
lm = create_lm(ui_profile)
dspy.configure(lm=lm)

logger.debug("Provisioning FactRepository, CelexDatabaseToolkit, HybridSearch")
fact_repo = FactRepository()
toolkit = CelexDatabaseToolkit(fact_repo=fact_repo)
chunk_repo = toolkit.chunk_repo
aggregator = FactAggregator(engine=fact_repo.engine)
# The UI's own reranker profile (settings.UI_RERANKER_BACKEND), so the batch
# CLIs can keep the heavy cross-encoder while this service stays light.
ui_ranker = ui_ranker_profile()
logger.debug(
    f"Configuring reranker ({ui_ranker.backend}: {ui_ranker.model_id or 'bundled'})"
)
ranker = create_ranker(ui_ranker)
hybrid_search = HybridSearch(
    repo=toolkit.chunk_repo, ranker=ranker, embed_query=toolkit.embed_query
)
# Share the fact repository's connection pool (Provisioner pattern) so answers
# can be annotated with a provenance trail.
lineage_repo = LineageRepository(engine=fact_repo.engine)
knowledge_repo = KnowledgeRepository()
# Backs the graph_search tool for both planners. Shares the same pool; None
# when the feature is off, which leaves the tool reporting itself unavailable.
entity_graph_repo = (
    EntityGraphRepository(engine=fact_repo.engine)
    if settings.GRAPH_SEARCH_ENABLED
    else None
)

# Pre-plan "answered before?" probe over the stored question variants; uses the
# toolkit's cached qwen3 embedder, the model the variants were ingested with.
knowledge_probe = (
    KnowledgeProbe(matcher=knowledge_repo, embed_query=toolkit.embed_query)
    if settings.KNOWLEDGE_PROBE_ENABLED
    else None
)

logger.debug("Building QARetrievalPipeline")
pipeline = QARetrievalPipeline(
    sql_store=fact_repo,
    kb_searcher=toolkit,
    aggregator=aggregator,
    hybrid_searcher=hybrid_search,
    doc_searcher=toolkit,
    lineage_repo=lineage_repo,
    chunk_loader=chunk_repo,
    knowledge_searcher=knowledge_repo,
    graph_searcher=entity_graph_repo,
    knowledge_probe=knowledge_probe,
)
logger.info("QARetrievalPipeline ready")

logger.debug("Building EnumerationFactModule")
enumeration_search = EnumerationSearch(repo=toolkit.chunk_repo, ranker=ranker,  embed_query=lambda q: [0.0])
enumeration_module = EnumerationFactModule(
    sql_store=fact_repo, searcher=enumeration_search
)
logger.info("EnumerationFactModule ready")

MODEL_NAME = "celex-dspy-agent"

# ---------------------------------------------------------------------------
# Conversation-history celex lookup — replaces the old global _active_celex
# ---------------------------------------------------------------------------
# Instead of a mutable global dict (which suffered cross-request races), we
# now derive the active case from the conversation messages that Open WebUI
# already sends with each request.  This is stateless and race-free.


class ChatMessage(BaseModel):
    """One turn of an OpenAI-compatible chat transcript.

    Attributes:
        role: Originator of the turn -- ``user``, ``assistant`` or ``system``.
        content: The turn's text as the client sends (and replays) it.
    """

    role: str = Field(description="Originator of the turn: user/assistant/system.")
    content: str = Field(description="Verbatim text of the turn.")


class ChatCompletionRequest(BaseModel):
    """Body of a ``POST /v1/chat/completions`` call.

    Attributes:
        model: Model id the client selected; only ``MODEL_NAME`` is served.
        messages: The conversation so far, oldest first. Constrained to at
            least one entry because the handler answers the *last* message --
            an empty list has no question to answer and is rejected at the
            boundary as a 422 rather than failing inside the handler.
        stream: Whether the answer is delivered as Server-Sent Events. Defaults
            to ``False`` like the OpenAI API it mimics, because a client that
            wants a single JSON completion simply omits the field -- the OpenAI
            SDK never sends it unless ``stream=True`` was passed. Open WebUI
            sets it explicitly on both paths (``True`` for chat, ``False`` for
            its background title/tag tasks), so both shapes are exercised.
    """

    model: str = Field(description="Requested model id.")
    messages: List[ChatMessage] = Field(
        min_length=1,
        description="Conversation turns, oldest first; the last one is the question.",
    )
    stream: bool = Field(
        default=False,
        description="True for a text/event-stream response, False for one JSON completion.",
    )


_RESET_PHRASES = frozenset(
    {
        "reset",
        "unset",
        "celex all",
        "set celex all",
        "celex reset",
        "celex null",
        "reset celex",
        "reset celex_id",
        "reset case",
        "celex_id null",
        "celex_id reset",
    }
)

# Framing words that wrap a "set the case context" command without adding a
# question (e.g. "set celex to <id>", "case <id>"). When the prompt contains
# only these plus a resolved case ID, the user is selecting context, not asking.
_CELEX_SET_FILLERS = frozenset(
    {
        "set", "celex", "celex_id", "celexid", "case", "to", "use", "for",
        "switch", "select", "context", "the", "id", "this", "please", "on",
    }
)


def _is_celex_only_prompt(question: str, candidate: str | None) -> bool:
    """Return True when the prompt only sets a case context, asking nothing.

    A prompt like ``"CELEX-31969D0216"`` or ``"set celex to 37766_610_5"`` names
    a case but poses no question. After the resolved case-ID candidate and the
    framing filler words are removed, no question content remains — so the
    pipeline should acknowledge the context switch instead of planning,
    synthesizing an answer, or attaching provenance.

    Parameters
    ----------
    question:
        The raw user prompt.
    candidate:
        The case/celex token that resolved (``CelexValidationResult.candidate``).

    Returns
    -------
    bool
        ``True`` when only the case ID and framing words are present.
    """
    if not candidate:
        return False
    remainder = question.lower().replace(candidate.lower(), " ")
    meaningful = [
        token
        for raw in remainder.split()
        if (token := raw.strip(".,;:!?=\"'`()-")) and token not in _CELEX_SET_FILLERS
    ]
    return not meaningful


def _find_celex_in_history(messages: List[ChatMessage]) -> str | None:
    """Scan prior *user* messages (newest → oldest) for a celex/case-ID.

    Returns the first canonical celex_id that resolves in the DB, or None.
    Uses the same registry-backed detection and validation as
    ``CelexSelectionModule`` so behaviour is identical to what the user
    experienced on the original turn.

    Stops scanning at any reset command so that a user-initiated context
    clear is honoured (messages before the reset are not considered).
    """
    # Only look at messages *before* the current one, newest first.
    prior = [m for m in messages[:-1] if m.role == "user"]
    for msg in reversed(prior):
        # Stop at a reset command — user explicitly cleared context.
        if msg.content.strip().lower() in _RESET_PHRASES:
            return None
        candidates = pipeline.celex_selector.extract_candidates(msg.content)
        for cand in candidates:
            try:
                resolved = pipeline.celex_selector.celex_checker.check_celex_or_case_id(
                    cand
                )
            except Exception:
                continue
            if resolved and resolved != "NA":
                return resolved
    return None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="CELEX DSPy QA Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Open WebUI calls this to discover available models
@app.get("/models")
@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {"id": MODEL_NAME, "object": "model", "owned_by": "dspy"},
        ],
    }


# Standalone deep-link to a provenance chunk's source text.  The chat UI now
# embeds chunk text inline (collapsible ``view chunk`` section rendered by
# ``_format_reference_block``), but this endpoint remains for direct
# ``GET /chunk/{chunk_id}`` access (e.g. ``verify_chunk_lookup``).
@app.get("/chunk/{chunk_id}", response_class=HTMLResponse)
def view_chunk(chunk_id: str) -> HTMLResponse:
    """Return the raw source text of a provenance chunk as a readable page.

    Args:
        chunk_id: Stable chunk id (``{embedding_table}.id``) from a provenance
            ``view chunk`` link.

    Returns:
        An HTML page showing the chunk's CELEX id, ``doc_pos`` and verbatim
        document text, or a 404 page when no chunk matches.
    """
    chunk = chunk_repo.get_chunk_by_id(chunk_id)
    if chunk is None:
        body = f"<h1>Chunk not found</h1><p><code>{html.escape(chunk_id)}</code></p>"
        return HTMLResponse(content=_html_page(body), status_code=404)

    doc_pos = chunk["doc_pos"]
    position = f" &middot; doc_pos {doc_pos}" if doc_pos is not None else ""
    body = (
        f"<h1>Source chunk</h1>"
        f"<p><code>{html.escape(chunk['celex_id'] or '')}</code>{position}</p>"
        f"<pre>{html.escape(chunk['document'] or '')}</pre>"
    )
    return HTMLResponse(content=_html_page(body))


def _html_page(body: str, title: str = "Source chunk") -> str:
    """Wrap *body* HTML in a minimal, readable standalone document.

    Args:
        body: Inner HTML to place inside ``<body>``.
        title: Document ``<title>`` text.

    Returns:
        A complete HTML document string.
    """
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:48rem;"
        "margin:2rem auto;padding:0 1rem;color:#1a1a1a}"
        "pre{white-space:pre-wrap;word-wrap:break-word;background:#f6f6f6;"
        "padding:1rem;border-radius:6px}code{background:#f0f0f0;padding:.1rem .3rem;"
        "border-radius:3px}img{max-width:100%;height:auto}"
        "table{border-collapse:collapse}"
        "th,td{border:1px solid #ccc;padding:.3rem .6rem}</style></head>"
        f"<body>{body}</body></html>"
    )


# ---------------------------------------------------------------------------
# Source-document viewer — render a case's original PDF and mineru markdown so
# Open WebUI can open them in a separate tab, addressed by ``celex_id``.
# ---------------------------------------------------------------------------
# The on-disk layout (relative to the configurable ``settings.CELEX_DOCS_DIR``):
#   <CELEX_DOCS_DIR>/<celex_id>/<celex_id>.pdf
#   <CELEX_DOCS_DIR>/<celex_id>/mineru/auto/<celex_id>.md

_CELEX_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_DOCS_ROOT = Path(settings.CELEX_DOCS_DIR).resolve()
_markdown_renderer = MarkdownIt("commonmark").enable("table")


def _resolve_doc_path(celex_id: str, *, kind: str) -> Path:
    """Resolve a ``celex_id`` to its on-disk pdf/md path, rejecting traversal.

    Args:
        celex_id: Case/CELEX identifier naming the document directory.
        kind: ``"pdf"`` or ``"md"`` selecting which artifact to resolve.

    Returns:
        The absolute path to the requested document (which may not exist).

    Raises:
        HTTPException: 400 when ``celex_id`` is malformed or would escape the
            configured docs root.
    """
    if not _CELEX_ID_PATTERN.fullmatch(celex_id):
        raise HTTPException(status_code=400, detail="invalid celex_id")
    case_dir = _DOCS_ROOT / celex_id
    target = (
        case_dir / f"{celex_id}.pdf"
        if kind == "pdf"
        else case_dir / "mineru" / "auto" / f"{celex_id}.md"
    )
    # Defence in depth: the resolved path must stay under the docs root.
    if not target.resolve().is_relative_to(_DOCS_ROOT):
        raise HTTPException(status_code=400, detail="invalid celex_id")
    return target


def _doc_not_found(celex_id: str, kind: str) -> HTMLResponse:
    """Return a 404 HTML page for a missing source document."""
    body = (
        f"<h1>Document not found</h1>"
        f"<p>No {html.escape(kind)} for <code>{html.escape(celex_id)}</code>.</p>"
    )
    return HTMLResponse(content=_html_page(body, title="Document not found"), status_code=404)


def _doc_footer(celex_id: str) -> str:
    """Build the markdown footer linking to a case's source documents.

    Args:
        celex_id: Canonical celex_id of the active case.

    Returns:
        A markdown footer with Markdown and PDF icon links pointing at the
        ``/doc/{celex_id}/md`` and ``/doc/{celex_id}/pdf`` viewer endpoints,
        or the empty string when no case is set.
    """
    if not celex_id:
        return ""
    base = settings.CHUNK_VIEW_BASE_URL.rstrip("/")
    return (
        f"\n\n---\n"
        f"[\U0001F4C4 MD]({base}/doc/{celex_id}/md) "
        f"[\U0001F4D5 PDF]({base}/doc/{celex_id}/pdf)\n"
    )


@app.get("/doc/{celex_id}", response_class=HTMLResponse)
def view_document(celex_id: str) -> HTMLResponse:
    """Render a tabbed viewer for a case's markdown and PDF source documents.

    Args:
        celex_id: Case/CELEX identifier addressing the document directory.

    Returns:
        An HTML page with Markdown and PDF tabs, each embedding the
        corresponding ``/doc/{celex_id}/md`` and ``/doc/{celex_id}/pdf`` route
        in an iframe.

    Raises:
        HTTPException: 400 when ``celex_id`` is malformed.
    """
    # Validate the id so the viewer fails fast on a bad parameter.
    _resolve_doc_path(celex_id, kind="md")
    safe_id = html.escape(celex_id)
    body = (
        f"<h1><code>{safe_id}</code></h1>"
        "<div class='tabs'>"
        "<button id='tab-md' class='active' onclick=\"show('md')\">Markdown</button>"
        "<button id='tab-pdf' onclick=\"show('pdf')\">PDF</button>"
        "</div>"
        f"<iframe id='frame-md' src='./{safe_id}/md'></iframe>"
        f"<iframe id='frame-pdf' src='./{safe_id}/pdf' style='display:none'></iframe>"
        "<style>body{margin:0;max-width:none;padding:0}"
        ".tabs{display:flex;gap:.25rem;padding:.5rem;background:#f0f0f0}"
        ".tabs button{font:inherit;padding:.4rem 1rem;border:0;border-radius:6px;"
        "background:#ddd;cursor:pointer}.tabs button.active{background:#1a1a1a;color:#fff}"
        "iframe{border:0;width:100%;height:calc(100vh - 3rem)}</style>"
        "<script>function show(k){"
        "var other=k==='md'?'pdf':'md';"
        "document.getElementById('frame-'+k).style.display='block';"
        "document.getElementById('frame-'+other).style.display='none';"
        "document.getElementById('tab-'+k).classList.add('active');"
        "document.getElementById('tab-'+other).classList.remove('active');}</script>"
    )
    return HTMLResponse(content=_html_page(body, title=f"{celex_id} — source documents"))


@app.get("/doc/{celex_id}/md", response_class=HTMLResponse)
def view_document_markdown(celex_id: str) -> HTMLResponse:
    """Render the mineru-extracted markdown for a case as a styled HTML page.

    Args:
        celex_id: Case/CELEX identifier addressing the document directory.

    Returns:
        An HTML page with the rendered markdown, or a 404 page when the markdown
        file is absent.

    Raises:
        HTTPException: 400 when ``celex_id`` is malformed.
    """
    md_path = _resolve_doc_path(celex_id, kind="md")
    if not md_path.is_file():
        return _doc_not_found(celex_id, "markdown")
    rendered = _markdown_renderer.render(md_path.read_text(encoding="utf-8"))
    return HTMLResponse(content=_html_page(rendered, title=f"{celex_id} — markdown"))


@app.get("/doc/{celex_id}/pdf")
def view_document_pdf(celex_id: str):
    """Serve the original PDF for a case inline.

    Args:
        celex_id: Case/CELEX identifier addressing the document directory.

    Returns:
        A ``FileResponse`` streaming the PDF inline, or a 404 HTML page when the
        PDF is absent.

    Raises:
        HTTPException: 400 when ``celex_id`` is malformed.
    """
    pdf_path = _resolve_doc_path(celex_id, kind="pdf")
    if not pdf_path.is_file():
        return _doc_not_found(celex_id, "PDF")
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{celex_id}.pdf"'},
    )


# Wire vocabulary of the Server-Sent-Events envelope.  Single-sourced because
# the non-streamed branch decodes exactly what the streamed branch encodes.
_SSE_DATA_PREFIX = "data: "
_SSE_DONE_PAYLOAD = "[DONE]"
_SSE_DONE = f"{_SSE_DATA_PREFIX}{_SSE_DONE_PAYLOAD}\n\n"
_SSE_MEDIA_TYPE = "text/event-stream"


def _sse_chunk(chunk_id: str, content: str) -> str:
    """Build a single SSE data line with a content delta."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }
    return f"{_SSE_DATA_PREFIX}{json.dumps(payload)}\n\n"


def _enqueue_for_knowledge_extraction(
    user_prompt: str,
    agent_answer: str,
    evidence: str,
    supporting_fact_ids: list[str] | None = None,
    supporting_chunk_ids: list[str] | None = None,
) -> None:
    """Fire-and-forget INSERT into ``knowledge_extraction_queue``.

    No LLM call — just a single SQL INSERT.  The batch job
    ``Main_ingest_knowledge.py`` picks up pending rows later.

    A producer-side stage filter (Stage 1 structural rejects) drops UI
    meta-tasks, control commands, error templates, empty / null-result
    answers, corpus aggregates, and answers retrieved without any fact (which
    the ingestor can never ground) before they ever enter the queue.  A pair
    already queued under the same normalised prompt and answer is skipped.
    """
    verdict = apply_stage_filter(
        user_prompt, agent_answer, fact_ids=supporting_fact_ids or []
    )
    if not verdict.kept:
        logger.info(
            "Skipping knowledge enqueue (stage_filter=%s): %s",
            verdict.reason,
            user_prompt[:80],
        )
        return

    duplicate_id = knowledge_repo.find_queued_duplicate(
        agent_answer, normalize_prompt(user_prompt), normalize_prompt
    )
    if duplicate_id is not None:
        logger.info(
            "Skipping knowledge enqueue (duplicate of %s): %s",
            duplicate_id,
            user_prompt[:80],
        )
        return

    with PgSession() as session:
        session.add(
            KnowledgeExtractionQueue(
                user_prompt=user_prompt,
                agent_answer=agent_answer,
                supporting_facts=evidence,
                fact_ids=supporting_fact_ids,
                chunk_ids=supporting_chunk_ids,
            )
        )
        session.commit()
    logger.info("Enqueued knowledge extraction for prompt: %s", user_prompt[:80])


# Inject the knowledge-extraction sink into the pipeline's QA branch, then build
# the single orchestration core.  The UI is now a thin shell over its events.
pipeline.enqueue_fn = _enqueue_for_knowledge_extraction

# Master Planner fallback — escalation for compositional queries the QA
# pipeline cannot answer.  Reuses the already-provisioned repositories and a
# per-row evidence verifier; gated by settings so it can be disabled via env.
master_planner = None
if settings.MASTER_PLANNER_ENABLED:
    logger.debug("Building MasterPlannerModule")
    evidence_repo = FactEvidenceRepository(engine=fact_repo.engine)
    row_verifier = EvidenceVerifierModule(
        chunk_repo=chunk_repo,
        lineage_repo=lineage_repo,
        fact_repo=fact_repo,
        evidence_repo=evidence_repo,
        query_embedding_repo=getattr(toolkit, "_query_repo", None),
    )
    master_planner = MasterPlannerModule(
        sql_store=fact_repo,
        kb_searcher=toolkit,
        aggregator=aggregator,
        hybrid_searcher=hybrid_search,
        doc_searcher=toolkit,
        knowledge_searcher=knowledge_repo,
        graph_searcher=entity_graph_repo,
        verifier=row_verifier,
    )
    logger.info("MasterPlannerModule ready")
else:
    logger.info(
        "MasterPlannerModule disabled via settings.MASTER_PLANNER_ENABLED; "
        "compositional queries will not escalate beyond the QA pipeline"
    )

logger.debug("Building RetrievalRouterModule")
orchestrator = RetrievalRouterModule(
    qa_pipeline=pipeline,
    enumeration=enumeration_module,
    celex_checker=pipeline.celex_selector.celex_checker,
    master_planner=master_planner,
)
logger.info("RetrievalRouterModule ready")


def stream_generator(question: str, messages: List[ChatMessage] | None = None):
    """Run the DSPy pipeline and yield OpenAI-compatible SSE chunks.

    Instead of a single "Planning retrieval …" indicator, we drive the
    sub-modules one-by-one and stream a status line before each stage so
    the user sees real-time progress:

        ⏳ Validating case reference …
        ⏳ Building retrieval plan …  (N steps)
        ⏳ Executing retrieval …      (tool list)
        ⏳ Synthesizing answer …

    Args:
        question: The current user question (last message content).
        messages: Full conversation history from the OpenAI-compatible
            request. Used to derive the active celex_id for follow-up
            questions that don't explicitly mention a case, and — when
            ``settings.CONVERSATION_HISTORY_ENABLED`` — to resolve a dependent
            follow-up ("list those cases") against the earlier turns.
    """
    chunk_id = f"chatcmpl-{int(time.time())}"
    messages = messages or []

    # -- Reset commands — inform user (history-based, no global state) ---
    normalised = question.strip().lower()
    if normalised in _RESET_PHRASES:
        # No plan, no synthesis, no LLM call — just acknowledge and break.
        yield _sse_chunk(chunk_id, "🔄 `celex_id` set to empty — context cleared.\n")
        done_json = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": MODEL_NAME,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"{_SSE_DATA_PREFIX}{json.dumps(done_json)}\n\n"
        yield _SSE_DONE
        return

    try:
        # The orchestrator owns the whole flow — gate → sticky promotion →
        # enumeration / passthrough / plan-execute-synthesize → provenance.
        # The UI is a thin shell: history parsing (request-shaped) stays here
        # and the resolved active case is handed in as ``fallback_celex_id``;
        # everything else is just mapping the orchestrator's events to SSE.
        sticky_id = _find_celex_in_history(messages)
        # Completed prior turns, with UI furniture stripped and each turn's
        # resolved case set recovered from its marker.
        turns = parse_history(messages)
        answer = ""
        active_celex_id = ""
        resolved_celex_ids: list[str] = []
        for event in orchestrator.run_events(
            question, fallback_celex_id=sticky_id, history=turns
        ):
            if event.kind == "status":
                yield _sse_chunk(chunk_id, event.text)
            elif event.kind == "case_context_set":
                # Prompt only selected a case — acknowledge and stop (no answer).
                yield _sse_chunk(chunk_id, event.text)
                celex_id = event.prediction.celex_validation.canonical_celex_id
                yield _sse_chunk(chunk_id, _doc_footer(celex_id))
                done_json = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": MODEL_NAME,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"{_SSE_DATA_PREFIX}{json.dumps(done_json)}\n\n"
                yield _SSE_DONE
                return
            elif event.kind == "result" and event.prediction is not None:
                answer = event.prediction.answer
                active_celex_id = event.prediction.celex_validation.canonical_celex_id
                resolved_celex_ids = list(
                    getattr(event.prediction, "celex_ids", []) or []
                )

    except Exception as exc:
        logger.exception("Pipeline failed")
        answer = f"⚠️ Pipeline error: {exc}"

    # Stream the answer in ~200-char chunks for a smooth typing effect
    chunk_size = 200
    for i in range(0, len(answer), chunk_size):
        fragment = answer[i : i + chunk_size]
        yield _sse_chunk(chunk_id, fragment)

    # Source-document footer — no-op when no case was resolved this turn.
    yield _sse_chunk(chunk_id, _doc_footer(active_celex_id))

    # Pin this turn's case set into the transcript.  The marker renders as
    # nothing, and Open WebUI replays the assistant message verbatim, so the
    # next turn can scope a refinement to exactly these cases with no
    # server-side state to key, lose on restart, or race on.
    if not resolved_celex_ids and active_celex_id:
        resolved_celex_ids = [active_celex_id]
    marker = encode_pinned_context(resolved_celex_ids)
    if marker:
        yield _sse_chunk(chunk_id, marker)

    # End-of-stream
    done_json = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"{_SSE_DATA_PREFIX}{json.dumps(done_json)}\n\n"
    yield _SSE_DONE


def _empty_completion_stream(reason: str):
    """Yield a well-formed, empty OpenAI SSE completion.

    Used to answer a call the pipeline must not run.  The client gets a valid
    (if contentless) completion rather than an error, so Open WebUI simply
    falls back to its own default for whatever it was generating.

    Args:
        reason: Logged explanation for the short-circuit.

    Yields:
        SSE lines terminating the stream immediately.
    """
    logger.info("Short-circuiting chat completion: %s", reason)
    done_json = {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"{_SSE_DATA_PREFIX}{json.dumps(done_json)}\n\n"
    yield _SSE_DONE


def _collect_completion(frames: Iterable[str]) -> dict:
    """Fold a finished SSE frame sequence into one non-streamed completion.

    The streamed and non-streamed answers must never diverge, so rather than
    running a second copy of the pipeline this decodes exactly what the SSE
    producer encoded: the message content is the concatenation of every
    ``delta.content``, which is the same text a streaming client renders --
    progress lines, source-document footer and pinned-context marker included.

    Args:
        frames: The raw SSE strings a generator such as :func:`stream_generator`
            yields, consumed to exhaustion.

    Returns:
        An OpenAI ``chat.completion`` object with a single assistant choice.

    Raises:
        json.JSONDecodeError: When a frame is not valid JSON, which would be a
            defect in the producer rather than in the request.
    """
    completion_id = f"chatcmpl-{int(time.time())}"
    content: list[str] = []
    finish_reason = "stop"

    for frame in frames:
        for block in frame.split("\n\n"):
            payload = block.strip()
            if not payload.startswith(_SSE_DATA_PREFIX):
                continue
            payload = payload.removeprefix(_SSE_DATA_PREFIX)
            if payload == _SSE_DONE_PAYLOAD:
                continue
            chunk = json.loads(payload)
            completion_id = chunk.get("id", completion_id)
            choice = chunk["choices"][0]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "".join(content)},
                "finish_reason": finish_reason,
            }
        ],
    }


def _completion_response(
    frames: Iterator[str], *, stream: bool
) -> StreamingResponse | JSONResponse:
    """Deliver a completion in the shape the client asked for.

    Args:
        frames: The SSE producer for this turn.
        stream: The request's ``stream`` flag.

    Returns:
        A ``StreamingResponse`` of SSE frames when *stream* is true, otherwise a
        ``JSONResponse`` carrying the aggregated ``chat.completion`` object.
    """
    if stream:
        return StreamingResponse(frames, media_type=_SSE_MEDIA_TYPE)
    # The handler is a sync ``def``, so FastAPI already runs it in a worker
    # thread -- draining the blocking pipeline generator here is safe and keeps
    # the event loop free.
    return JSONResponse(content=_collect_completion(frames))


# Open WebUI calls this to chat
@app.post("/chat/completions")
@app.post("/v1/chat/completions")
def chat_completions(request: ChatCompletionRequest):
    logger.info(
        "Request model=%s, messages=%d, stream=%s",
        request.model,
        len(request.messages),
        request.stream,
    )
    user_message = request.messages[-1].content

    # Open WebUI fires background completions against this same endpoint for
    # title, tag and follow-up generation.  They are not questions: running the
    # retrieval pipeline on them burns DGX inference and would pin a bogus case
    # set onto the conversation.  Reject them before anything else happens.
    # These are exactly the calls that arrive with ``stream: false``.
    if settings.WEBUI_REJECT_TASK_PROMPTS and is_meta_task_prompt(user_message):
        return _completion_response(
            _empty_completion_stream(f"UI meta-task prompt: {user_message[:60]}"),
            stream=request.stream,
        )

    return _completion_response(
        stream_generator(user_message, messages=request.messages),
        stream=request.stream,
    )
