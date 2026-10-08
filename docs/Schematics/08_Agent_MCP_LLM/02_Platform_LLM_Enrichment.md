# Platform LLM: LLMService, Enrichment, Parser Refine

> **⚠️ Superseded 2026-09-09 for the direct query path.** The enrichment
> research loop in §2 (LLM selects `nameSearch`/`structuredSearch`, they
> execute, LLM synthesizes) still exists as `QueryEnrichmentService.enrich()`
> for the agent path, but the **direct path** (`execute-query/stream`) now
> uses `EntityContextService.get_context()` (deterministic USLP/communities/
> classes SQL) + `QueryEnrichmentService.synthesize()`, **one LLM call, no
> tool selection**. See `docs/plans/completed/DIRECT_PATH_ENRICHMENT_PLAN.md`
> §13 for the before/after diagram and the full implemented state.
>
> **Focus:** how the local model (qwen3:8b on Ollama, switched 2026-09-16)
> is wired into the
> Django platform through a provider-abstracted client, and how it enriches
> answers by doing research with the toolset.
>
> **Key idea:** the LLM is an **accelerator, never a single point of
> failure**. Every integration is fail-soft (`None`/`False` on any error)
> with a deterministic fallback, and the abstraction is one env var away
> from a cloud provider. Enrichment follows the "agent does research"
> pattern: the LLM selects research tools, they execute deterministically
> against the real endpoints, and the LLM synthesizes a grounded answer
> that always includes the primary entities.

---

## 1. LLMService: `core/services/llm_service.py`

Two API styles, selected by `LLM_API_STYLE`:

- **`native` (default)**: Ollama's `/api/chat` with `think: false`. qwen3
  models default to thinking mode, and the reasoning trace eats the token
  budget on the OpenAI-compatible endpoint (which ignores `think`), leaving
  `content` empty. The native endpoint honors `think: false`; `chat_json`
  also sends `format: "json"` for strict structured output.
- **`openai`**: `/v1/chat/completions` + `/v1/embeddings` for cloud
  providers (DeepSeek/OpenAI). Swap by setting `LLM_API_STYLE=openai` +
  `LLM_BASE_URL`.

Methods: `is_available()` (cached liveness probe, `GET /api/tags`),
`chat()`, `chat_json()` (JSON extraction tolerating fences/prose),
`chat_stream()` (token deltas, native NDJSON or openai SSE), `embed()`.
Built on `requests` (no SDK, the backend container is Python 3.8 and modern
`openai`/`mcp` SDKs have dropped 3.8).

Config: `LLM_ENABLED`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_STYLE`,
`LLM_TIMEOUT` (default 60, cold model reloads take 5-15s), availability
cache TTL.

---

## 2. Enrichment Research Loop: `query_enrichment_service.py`

After a template result (e.g. "Found 18 entities within 50km."):

```
primary result → LLM selects 1-2 tools → execute deterministically
  → LLM synthesizes grounded answer (primary digest + research outputs)
```

1. **Selection**: `chat_json` asks the LLM for
   `{"tools": [{"tool": "nameSearch", "args": {...}}, ...]}`; validated
   against `RESEARCH_TOOLS = {nameSearch, structuredSearch}` (max 2, args
   required).
2. **Forced default**: if the LLM selects nothing usable, a default action
   is derived from the result set (dominant amenity → `structuredSearch`,
   else top named entity → `nameSearch`). Research is never skipped when the
   model is up.
3. **Execution**: `_call_search_tool` runs the real endpoint via Django's
   test client (`HTTP_HOST="localhost"`, the default `testserver` host is
   rejected by `ALLOWED_HOSTS`), returning compact `{name, wkg_class, lat,
   lon, score}` lists.
4. **Synthesis**: the prompt always includes a **primary digest** (class
   counts + top named entities with distances) plus the research outputs, so
   the answer is grounded even when research returns nothing. Distances are
   shown in km AND meters (`Cafe A (6.2km / 6219m)`) with an explicit units
   rule. A production run showed the model comparing raw meters to a
   km-scale radius.
5. **Streaming**: with an `event_callback`, synthesis streams token deltas
   via `chat_stream` (`answer_delta` events) and research progress is
   emitted (`research`, `research_out`).
6. **Cache**: enriched answers are TTL-cached in-process
   (`LLM_ANSWER_CACHE_TTL_SECONDS`, default 1800s, keyed by
   question+country+snapshot). Hits replay events without LLM calls.

Response gains `enrichment: {primary_answer, actions, action_outputs,
enriched_answer}`. Any failure returns the deterministic answer unchanged.

---

## 3. Parser Refine: `query_parser_service.py`

`QueryParserService.parse()` re-classifies template + concepts via the LLM
when TF-IDF confidence < `MAPQA_LLM_FALLBACK_CONFIDENCE` (default 0.5). The
LLM's template is validated against the trained label set and concept types
against `CONCEPT_TYPES`; the deterministic "within X of" override is
re-applied; any failure keeps the heuristic parse.

---

## 4. LLM Call Counts

- **`skip_llm`**: `_synthesize_answer(..., skip_llm=True)` skips the
  standalone answer-rewrite LLM pass when a question is present, since the
  enrichment synthesis supersedes it.
- **Direct path**: exactly **1** LLM call per enriched request
  (`synthesize()`, no tool selection).
- **Agent path (`enrich()`)**: **2** calls (selection + synthesis) after
  `skip_llm` (down from 3).

---

## 5. Files

- `backend/core/services/llm_service.py`
- `backend/semantic_search/services/query_enrichment_service.py`
- `backend/semantic_search/services/query_parser_service.py` (`_llm_refine`)
- `backend/semantic_search/services/query_executor_service/`
  (`_synthesize_answer`, enrichment wiring in `execute()`)
- `backend/tests/unit/test_llm_service.py`,
  `backend/tests/unit/test_query_enrichment_service.py`

**Test:** `python3 -m pytest tests/unit/test_llm_service.py
tests/unit/test_query_enrichment_service.py` (LLM fully mocked, no Ollama
required).
