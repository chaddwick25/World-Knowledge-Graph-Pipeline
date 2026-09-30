"""Research orchestrator views (SSE stream, interviewer chat, brief finalize).

Extracted from ``worldkg_nca/views/search.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

import asyncio
import json
import logging
import threading
import uuid

from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code

logger = logging.getLogger(__name__)


@require_GET
def research_stream(request):
    """Streaming research orchestrator over SSE (Server-Sent Events).

    GET /api/nca/research/stream/?prompt=...&country_code=...&snapshot_date=...

    Emits progressive events so the frontend shows the loop working:
        event: plan           data: {questions}
        event: question       data: {index, question, template, answer,
                                     result_count, error?}
        event: tool           data: {tool, args}
        event: tool_out       data: {tool, output}
        event: summary_delta  data: {delta}
        event: done           data: {result}          (full result JSON)
        event: error          data: {error}

    Same transport as ``execute_query_stream``: the orchestrator runs in a
    worker thread; events flow through a queue to the response generator.
    """
    prompt_text = (request.GET.get("prompt") or "").strip()
    country_code = request.GET.get("country_code") or None
    snapshot_date = request.GET.get("snapshot_date") or None

    if not prompt_text:
        return JsonResponse({"error": "prompt required"}, status=400)

    # Traceability (UNIFIED_LLM_TRACE_PLAN.md): one trace per run; the
    # caller can propagate a cross-app id via ?trace_id= (or X-Trace-Id).
    trace_id = request.GET.get("trace_id") or uuid.uuid4().hex

    async def event_stream():
        from core.services.trace_service import TraceService

        loop = asyncio.get_running_loop()
        events = asyncio.Queue(maxsize=512)

        def emit(event, **payload):
            if isinstance(event, dict):
                payload = {k: v for k, v in event.items() if k != "event"}
                event = event.get("event") or "message"
            # Deterministic stage markers (behind TRACE_DETERMINISTIC_STAGES=1).
            TraceService.stage_event(event, payload)
            loop.call_soon_threadsafe(events.put_nowait, {"event": event, **payload})

        def run():
            try:
                from core.services.trace_service import TraceService
                from semantic_search.services.research_service import (
                    ResearchOrchestratorService,
                )
                cc = country_code
                if cc:
                    try:
                        cc = resolve_iso_code(cc)
                    except Exception:
                        pass  # use as-is if resolution fails
                # Thread-local trace: LLM spans from decompose/assemble/follow-ups
                # attach as children; question/tool events become point spans.
                with TraceService.run_trace(
                    "research", trace_id=trace_id,
                    metadata={
                        "prompt": prompt_text[:200], "country_code": cc,
                        "snapshot_date": snapshot_date,
                    },
                ):
                    result = ResearchOrchestratorService.plan(
                        prompt_text, cc, snapshot_date, event_callback=emit,
                    )
                    if isinstance(result, dict):
                        result["trace_id"] = trace_id
                    emit("done", result=result)
            except Exception as exc:  # noqa: BLE001 — surface errors as an SSE event
                emit("error", error=str(exc))
            finally:
                loop.call_soon_threadsafe(events.put_nowait, None)  # sentinel

        threading.Thread(target=run, daemon=True).start()

        while True:
            item = await events.get()
            if item is None:
                break
            yield f"event: {item['event']}\ndata: {json.dumps(item, default=str)}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    response["X-Trace-Id"] = trace_id
    return response


@csrf_exempt
@require_POST
def research_chat(request):
    """Stream the KE interviewer reply over SSE (AI SDK UI-message-stream).

    POST /api/nca/research/chat/
    body: {"messages": [AI SDK UIMessage dicts or {role, content}],
           "country_code": "BZ", "snapshot_date": "2025_12_31"}

    Emits the AI SDK v7 UI-message-stream protocol (frontend
    ``useChat``/``DefaultChatTransport`` consumes it):
        data: {"type": "start"}
        data: {"type": "text-start", "id": "t1"}
        data: {"type": "text-delta", "id": "t1", "delta": "..."}  (repeated)
        data: {"type": "text-end", "id": "t1"}
        data: {"type": "finish", "finishReason": "stop"}
    Headers include ``x-vercel-ai-ui-message-stream: v1`` (the protocol
    marker). The interviewer is the interactive LLM instance (the 4070):
    the conversation is the interactive path, separate from the batch
    orchestrator on the 2070. Fail-soft: 503 JSON when the LLM is down;
    the stream always terminates with a finish frame.

    ``@csrf_exempt`` matches the DRF views (DRF bypasses the global CSRF
    middleware; its SessionAuthentication only enforces CSRF for
    session-authenticated requests). This endpoint is stateless chat with
    no writes, and PublicAuthGuardMiddleware gates it on public hosts.
    """
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return JsonResponse({"error": "messages required"}, status=400)
    country_code = body.get("country_code") or None
    snapshot_date = body.get("snapshot_date") or None
    trace_id = body.get("trace_id") or uuid.uuid4().hex

    from core.services.llm_service import LLMService
    from semantic_search.services.research_service import (
        ResearchOrchestratorService,
    )

    llm = LLMService.get_instance()
    if not llm.is_available():
        return JsonResponse({"error": "interviewer LLM unavailable"}, status=503)

    full_messages = ResearchOrchestratorService.chat_messages(
        messages, country_code, snapshot_date,
    )

    def _part(part_dict):
        return f"data: {json.dumps(part_dict)}\n\n"

    async def event_stream():
        loop = asyncio.get_running_loop()
        events = asyncio.Queue(maxsize=512)

        def emit(line):
            loop.call_soon_threadsafe(events.put_nowait, line)

        def run():
            try:
                from core.services.trace_service import TraceService
                # One trace per interview turn; the KE stream span attaches
                # via the thread-local (LLMService.chat_stream).
                with TraceService.run_trace(
                    "research_chat", trace_id=trace_id,
                    metadata={
                        "country_code": country_code,
                        "snapshot_date": snapshot_date,
                    },
                ):
                    emit(_part({"type": "start"}))
                    emit(_part({"type": "text-start", "id": "t1"}))
                    for delta in llm.chat_stream(
                        full_messages, temperature=0.4, max_tokens=400,
                    ):
                        if delta:
                            emit(_part({
                                "type": "text-delta", "id": "t1", "delta": delta,
                            }))
                    emit(_part({"type": "text-end", "id": "t1"}))
            except Exception as exc:  # noqa: BLE001 — never leave the client hanging
                logger.warning("Research chat stream failed: %s", exc)
            finally:
                emit(_part({"type": "finish", "finishReason": "stop"}))
                loop.call_soon_threadsafe(events.put_nowait, None)

        threading.Thread(target=run, daemon=True).start()

        while True:
            item = await events.get()
            if item is None:
                break
            yield item

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    response["X-Vercel-AI-UI-Message-Stream"] = "v1"
    response["X-Trace-Id"] = trace_id
    return response


@csrf_exempt
@require_POST
def research_finalize(request):
    """Extract a structured research brief from the interview conversation.

    POST /api/nca/research/finalize/
    body: {"messages": [...], "country_code": "BZ", "snapshot_date": "..."}

    Returns {"brief", "fields", "source"}:
      source "structured" — chat_json field extraction + deterministic
        render (the KE never emits free-form briefs, so the runaway
        place/distance list cannot recur)
      source "fallback"  — the first user message (LLM down / empty
        extraction)

    Fail-soft: no brief extractable → 422.
    """
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    messages = body.get("messages")
    if not isinstance(messages, list):
        return JsonResponse({"error": "messages required"}, status=400)
    country_code = body.get("country_code") or None
    snapshot_date = body.get("snapshot_date") or None

    from semantic_search.services.research_service import (
        ResearchOrchestratorService,
    )

    result = ResearchOrchestratorService.finalize_brief(
        messages, country_code, snapshot_date,
    )
    if not result:
        return JsonResponse({"error": "no brief extractable"}, status=422)
    return JsonResponse(result)
