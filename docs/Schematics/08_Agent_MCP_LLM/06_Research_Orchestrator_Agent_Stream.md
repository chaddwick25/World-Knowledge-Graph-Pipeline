# Research Orchestrator: Agent Stream

> **Focus:** how the Research tab turns one big prompt ("plan a 2-day trip
> to Belize City") into a grounded research summary: the KE interviewer on
> the 4070, the batch orchestrator on the 2070, the deterministic MapQA
> executor between them, and the SSE + traceability contract that makes
> the run observable end to end.
>
> **Key idea:** exactly one LLM is in the hot loop (the 2070 orchestrator).
> It reasons over deterministic primary answers; the final summary is the
> enriched deliverable. The 4070 interviewer is a side path and never
> enters the loop.
>
> **Processing model:** the current path is **stream processing** (on-demand
> request → SSE events as each phase completes), the K80 path is **batch
> processing** (offline-tolerant cadence, nobody waits), mapped from Chip
> Huyen, *Designing Machine Learning Systems* (O'Reilly, 2022), Ch 3 "Batch
> Processing Versus Stream Processing" and Ch 7 "Batch Prediction Versus
> Online Prediction" (§2). The loop contract is identical on both; only the
> cadence and latency budget change.
>
> **Status:** ✅ live 2026-09-18/19 (MVP 2026-09-18, KE chat + structured
> brief + trace UI 2026-09-18/19). Plan:
> `docs/plans/completed/RESEARCH_ORCHESTRATOR_MVP_PLAN.md`. User-facing
> test doc: `sample_research_prompts.md`. The K80 migration
> (`docs/plans/later-stages/K80_LLM_MIGRATION_PLAN.md`) replaces the 2070
> Ollama daemon with llama-server on K80 die 0; when it lands, this
> schematic gets a sibling with the K80 deltas (§7).

---

## 1. The agent flow

```
browser (ResearchPanel.vue, Home.vue "Research" tab)
  │
  │ interview: useChat / DefaultChatTransport
  │   ──► POST /api/nca/research/chat/ ──► KE interviewer (4070)
  │          (AI SDK v7 UI-message-stream, one clarifying question at a time)
  │
  │ brief: axios
  │   ──► POST /api/nca/research/finalize/ ──► chat_json extraction
  │          + deterministic render  (source: structured | fallback)
  │
  │ run: EventSource (GET)
  │   ──► GET /api/nca/research/stream/?prompt=…&country_code=…&snapshot_date=…
  ▼
orchestrator loop (2070, ollama-research:11435)
  │
  │ decompose   chat_json, temp 0 ──► {questions: [{question, why}]}
  │             (5-template manifest, max 6 questions)
  ▼
MapQA parser + executor (Django, deterministic, no GPU)
  │   execute × N, skip_enrichment=True
  │   anchor-qualifier retry ("the centre of Belize City" → "Belize City")
  │   "the top <class>" placeholder substitution from the prior question
  ▼
follow-up tools (RESEARCH_FOLLOWUP_TOOLS=1): 0-2 nameSearch / structuredSearch
  ▼
assemble   chat_stream, temp 0.3 ──► grounded summary (summary_delta stream)
  │
  ▼
SSE to browser: plan → question × N → [tool, tool_out] → summary_delta → done
```

Phase table:

| Phase | Runs on | LLM call | Output |
|---|---|---|---|
| Interview | 4070 (`LLMService.get_instance`) | `chat_stream` (temp 0.4, max 400) | one clarifying question per turn |
| Brief | Django (extraction uses the 4070) | `chat_json` (temp 0) + `_render_brief` | `{brief, fields, source}`; the KE never emits a brief itself |
| Decompose | 2070 (`get_research_instance`) | `chat_json` (temp 0, max 900) | 4-6 parser-ready questions |
| Execute | Django, no GPU | none | per question: `{template, confidence, answer, digest, result_count}` |
| Follow-ups | Django + search endpoints | `chat_json` (temp 0) picks tools | 0-2 tool calls, outputs join the summary prompt |
| Assemble | 2070 | `chat_stream` (temp 0.3, max 1200) | grounded summary, streamed |

The hot loop is orchestrator ↔ parser/executor. Loop pace equals 2070
reasoning speed; the 4070 slots never see research traffic.

Loop mechanics (all deterministic, all logged):

- **Anchor-qualifier retry**: "the centre of Belize City" geocodes null
  (`spatial_filter_skipped` in the trace) → strip the qualifier and re-run
  once with the plain name.
- **Placeholder substitution**: a later question may say "the top hotel";
  the loop substitutes the top entity name from the prior question's
  results (the question's own anchor is excluded; an empty result does not
  wipe the placeholder).
- **Small-radius empty escalation (2026-09-19)**: a #1 question with an
  explicit radius below `DEFAULT_NEAR_RADIUS_M` (2km) that returns zero
  results (anchor geocoded fine) is widened to 2km and re-run once ("1 km
  is too small to search Dublin"); the empty answer previously invited the
  assembler to invent content. The record and SSE `question` event carry
  `radius_escalated: true`. The decompose prompt also scales radii to the
  anchor (city ≥ 2km, prefer 3-5km; venue 500m-2km; never 1km for a city).

## 2. Stream processing vs batch processing (DMLS)

The processing model maps onto Chip Huyen's batch/stream distinction in
*Designing Machine Learning Systems* (O'Reilly, 2022). The relevant
sections: Ch 3 "Data Engineering Fundamentals", "Batch Processing Versus
Stream Processing", and Ch 7 "Model Deployment and Prediction Service",
"Batch Prediction Versus Online Prediction".

Huyen's split, in her own terms:

- **Batch**: jobs kicked off periodically process data in chunks. Batch
  prediction generates predictions offline in advance, stores them, and
  fetches them when a request arrives; asynchronous. "Batch prediction is a
  workaround for when your models take too long to generate predictions,
  but it makes your model less flexible" (Ch 7).
- **Stream**: processing happens as data or requests arrive. Online
  prediction is on-demand and synchronous; "latency is crucial". "Online
  prediction makes your model more responsive to users' changing
  preferences" (Ch 7).

The mapping here is on the LLM serving plane (inference requests, not data
rows): the current 2070 path is the stream path, the planned K80 die 0 is
the batch path. Huyen's Ch 3 note that "batch processing is a special case
of stream processing" holds: the K80 runs the same loop at a slower cadence.

| Dimension | Stream path (current, RTX 2070) | Batch path (planned, K80 die 0) |
|---|---|---|
| Cadence | on-demand: one request → one run | triggered, nobody waits (K80 plan: "nobody waits on the batch path, so 30-60s per reasoning step is acceptable") |
| Latency budget | interactive, ~1-2 min/run, events fire as each phase completes | 300-400-token LLM calls at ~40-55s each; `LLM_TIMEOUT` must rise above the 60s default |
| Delivery | SSE: plan → question × N → summary_delta, live | same SSE protocol, longer gaps between events |
| Responsiveness | user watches and revises (second pass) | fixed brief, results arrive later; less flexible, Huyen's trade-off verbatim |
| Decode | Ollama qwen3:8b, ~30-40 t/s (448 GB/s) | llama-server Qwen3-8B GGUF, K80 ~240 GB/s bandwidth-bound; `--parallel 2` ceiling |

The invariant that survives both paths: **one LLM in the hot loop,
reasoning over deterministic primary answers**. Decompose → execute →
assemble, the SSE event protocol, and the traceability contract (§4-§5)
are identical; only the cadence and the latency budget change.

The system is already a hybrid in Huyen's Ch 7 sense (unifying batch and
streaming pipelines): each question's execution is deterministic,
batch-like aggregation (the whole question is processed at once, results
digested), while delivery streams and the interview is interactive. The
stream is in the delivery and the loop's interactivity; the batch is in
each deterministic question's execution.

## 3. Two LLM instances on the Docker network via Ollama

`docker-compose.yml` declares no `networks:` key, so every service sits on
the compose default bridge network. The backend resolves both daemons by
service name: `ollama` (4070) at `http://ollama:11434/v1` and
`ollama-research` (2070) at `http://ollama-research:11435/v1`. Local
(non-Docker) dev uses `localhost:11434` / `localhost:11435`.

| Daemon | Card | Port | Backend env | Purpose |
|---|---|---|---|---|
| `ollama` | RTX 4070 Ti SUPER (16 GB, `NVIDIA_VISIBLE_DEVICES=1`) | 11434 | `LLM_BASE_URL`, `LLM_MODEL=qwen3:8b` | interactive: KE interviewer, enrichment, parser refine |
| `ollama-research` | RTX 2070 (8 GB, `NVIDIA_VISIBLE_DEVICES=0`) | 11435 | `RESEARCH_LLM_BASE_URL`, `RESEARCH_LLM_MODEL=qwen3:8b`, `RESEARCH_LLM_TIMEOUT=120` | batch: decompose + assemble |

`LLMService` (`backend/core/services/llm_service.py`) is provider-abstracted
(native Ollama `/api/chat` with `think: false` and `format: "json"` for
`chat_json`, or any OpenAI-compatible `/v1`). Two instances:

- `get_instance()` reads `LLM_*`; the platform instance, unchanged.
- `get_research_instance()` reads `RESEARCH_LLM_*`; with
  `RESEARCH_LLM_BASE_URL` unset it returns `get_instance()`, so the loop
  tests on the 4070 before the 2070 split is configured. The env flip is
  the whole 2070 migration.

`ollama-research` runs `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_NUM_PARALLEL=1`,
`OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_MAX_QUEUE=8`, and a **separate models
volume** (`ollama_models_research`): two Ollama daemons on one models dir
lock each other's writes. qwen3:8b Q4 fits the 2070's 8 GB at ~6 GB
resident (1 slot / 4096 context), ~2 GB headroom.

## 4. SSE streaming

Three endpoints (`backend/worldkg_nca/urls.py`,
`backend/worldkg_nca/views/search.py`):

| Endpoint | Method | Transport | Protocol |
|---|---|---|---|
| `GET /api/nca/research/stream/` | GET | SSE | named events below |
| `POST /api/nca/research/chat/` | POST | SSE | AI SDK v7 UI-message-stream |
| `POST /api/nca/research/finalize/` | POST | JSON | `{brief, fields, source}`; 422 when nothing extractable |

`research_stream` event protocol:

```
plan          {questions}                        decompose done
question      {index, question, template, answer,
               digest, result_count, error?}      per executed question
tool          {tool, args}                        follow-up selected (opt-in)
tool_out      {tool, output}                      follow-up executed
summary_delta {delta}                             token stream, assemble
done          {result}                            full result JSON
error         {error}
```

Transport mechanics mirror `execute_query_stream`
(`03_SSE_Streaming_Performance.md`): the orchestrator runs in a worker
thread, events flow through an `asyncio.Queue(maxsize=512)` to the
`StreamingHttpResponse` (`Cache-Control: no-cache`,
`X-Accel-Buffering: no`). The view's `emit` normalizes both call forms
(`emit("name", key=val)` and the executor's dict-form
`event_callback({"event": ...})`); without it the whole dict lands in the
SSE event name and named listeners never fire.

`research_chat` speaks the AI SDK v7 UI-message-stream protocol
(`data:` frames: `start` → `text-start` → `text-delta` × N → `text-end` →
`finish`), with the `x-vercel-ai-ui-message-stream: v1` header; the
frontend `useChat`/`DefaultChatTransport` consumes it. 503 JSON when the
4070 is down; the stream always terminates with a finish frame.

Frontend consumption: `EventSource` (GET-only; the URL is built from
`axios.defaults.baseURL`, a relative `/nca/...` path hits Vite with no
proxy), same pattern as the search panel's template mode. The `done`
payload's `result` carries the complete run:
`{prompt, country_code, snapshot_date, questions, tool_calls, summary,
errors}`.

**Run traceability (2026-09-19)**: every stream run carries a `trace_id`
(one per run; a caller can propagate a cross-app id via `?trace_id=` or
the body). It appears in the `done` payload's `result` and in the
`X-Trace-Id` response header. The run executes inside
`TraceService.run_trace` (thread-local), so LLM spans emitted by
`LLMService` in the same worker thread attach as children
(`UNIFIED_LLM_TRACE_PLAN.md`).

## 5. Traceability

The SSE sequence is the run's audit trail: every decomposed question, its
template, answer, digest, and result count arrive as a named event, and
`done` replays the full result JSON. The panel renders the trail as an
Execution Trace block (same `details/summary` pattern as
SemanticSearchPanel):

- per-question cards with a numbered badge, template badge, and red
  `error` badge on failure;
- "(0 results)" when the executor found nothing ("No results found." with
  a zero count);
- value highlighting: entity names from the digest plus distance/amount
  tokens ("53.02 km", "(851 m)") rendered dark blue by `answerSegments`;
- a left spine that turns info-colored while the run streams; the block
  auto-collapses when the summary starts.

Per-question records carry the evidence the summary must be grounded in:
`{question, template, confidence, answer, digest, result_count, error?}`.
The `digest` is `QueryEnrichmentService._primary_digest(results)`: top
named entities with distances plus class counts. The assemble prompt
consumes exactly these records (via `_answers_text`) and is constrained to
ground every claim in them; an errored question is reported in the summary
as missing rather than invented.

Loop mechanics are logged, not silent:

- anchor-qualifier retry: "Research retry with stripped anchor: <alt>";
- placeholder substitution: "the top hotel" → the top entity name from the
  prior question's results (the question's own anchor is excluded, so the
  anchor never becomes "the top <class>"; an empty result does not wipe the
  placeholder);
- console diagnostics in the panel carry the `[Research]` prefix:
  `chat status`, `chat error`, `finalize: structured|fallback <brief>`,
  `run start, source: brief|lastUserPrompt`, `plan: N questions`,
  `question: <i> <template> count: <n> <error>`,
  `done, summary len: N elapsed: Xs errors: M`.

Fail-soft records, all surfaced in the trace and the `errors` list:

- orchestrator LLM down → `{questions: [], summary: None, errors:
  ["research LLM unavailable"]}`; a mid-loop failure returns the
  deterministic answers without a summary;
- one bad question never kills the loop; the summary is written from
  surviving answers;
- brief extraction failure or LLM down → `source: "fallback"` (the first
  user message, labeled "from your first message" in the brief box); a run
  with no brief falls back to `lastUserPrompt`.

### The unified trace adapter (developer-facing)

`TraceService` (`core/services/trace_service.py`) adds the
developer-facing observability layer under the SSE spine
(`UNIFIED_LLM_TRACE_PLAN.md`, implemented 2026-09-19): one schema
(`trace` + `span` events), pluggable sinks (none default | console |
file | langfuse | memory), SDK-free Langfuse HTTP ingestion (backend is
Python 3.8), bounded queue + batched daemon flush
(`TRACE_FLUSH_INTERVAL`, default 2s), drop-oldest, sampling. Two emit
layers: LLM spans from `LLMService` (every call, both instances,
model/temperature/token attributes) and the run trace from the views.
`TRACE_DETERMINISTIC_STAGES=1` adds per-question point spans via
`stage_event`. The `trace_id` in the `done` payload links the SSE run to
the trace. The K80 batch path inherits the adapter unchanged.

## 6. The MCP bridge: not in this flow

The Research tab does **not** use the vue-mcp-server bridge. `ResearchPanel.vue`
contains zero MCP references: the interview, brief, and run all talk to
Django directly (`POST /api/nca/research/chat/`, `POST
/api/nca/research/finalize/`, `GET /api/nca/research/stream/`). The MCP
surface (`/__mcp`, Streamable HTTP, browser channel) is the Goose agent's
tool palette (`01_MCP_Tool_Surface.md`); the Research tab is a first-party
UI and never routes through it.

Where the two touch: the loop's follow-up phase (opt-in
`RESEARCH_FOLLOWUP_TOOLS=1`) selects from `RESEARCH_TOOLS`
(`query_enrichment_service.py`) and executes via `_call_search_tool`. The
tool vocabulary, `nameSearch` and `structuredSearch`, mirrors the MCP
Node-side data tools of the same names (`server-tools.ts`): same names,
same semantics, separate call paths. The Goose agent reproduces
research-style chaining by calling the MCP tools (`templateQuery` +
`nameSearch` + `structuredSearch`); the Research tab performs the same
chaining server-side, in-process, with the orchestrator LLM in the loop
instead of the agent.

## 7. K80 follow-on (sibling schematic when it lands)

`K80_LLM_MIGRATION_PLAN.md` (later-stages, Planned): K80 die 0 takes the
orchestrator role via llama-server (OpenAI-compatible HTTP), not Ollama.
This is the batch path of §2: the flow contract does not change (same
endpoints, same SSE protocol, same traceability), only the cadence and the
latency budget. The sibling schematic in this directory must document the
deltas, and its opening **Processing model** block must state the
stream/batch contrast against this doc the way §2 does here:

- LLM instance config: llama-server URL/port instead of
  `ollama-research:11435`; the compose default network reachability still
  applies (service-name DNS);
- model/quant: Qwen3-8B GGUF, Q4_K_M / IQ4 family; NVFP4 quant never runs
  on Kepler;
- `--reasoning-budget 0` mandatory (the OpenAI-compatible endpoint ignores
  `think: false`; the thinking trace eats the 200-300 token budgets);
- decode budget: 300-400 token calls at ~40-55s, `LLM_TIMEOUT` must rise
  above the 60s default; `--parallel 2` is the ceiling;
- batch semantics: nobody waits, 30-60s per reasoning step acceptable,
  `OLLAMA_NUM_PARALLEL`-style slot reasoning replaced by llama-server
  `--parallel`;
- K80 die 1 takes pipeline compute (GV-NLE, USLP, spectral) pending the
  torch sm_37 verification gate.

## 8. Files

- `backend/semantic_search/services/research_service.py`,
  `ResearchOrchestratorService` (loop, KE chat, brief finalize)
- `backend/core/services/llm_service.py`, `get_research_instance()`
- `backend/worldkg_nca/views/search.py`, `research_stream`,
  `research_chat`, `research_finalize`
- `backend/worldkg_nca/urls.py`, research routes
- `frontend-v3/src/components/ResearchPanel.vue`, Research tab UI (the
  terminal `research_demo` command was removed 2026-09; this is the
  end-to-end surface)
- `docker-compose.yml`, `ollama-research` service + `RESEARCH_LLM_*` env
- `docs/plans/completed/RESEARCH_ORCHESTRATOR_MVP_PLAN.md`, implementation
  plan (moved from later-stages 2026-09-19)
- `sample_research_prompts.md`, user-facing test doc (worked Belize
  interview, resolved runaway-brief issue, test scenarios, console
  diagnostics, per-country starter prompts)
- `backend/tests/unit/test_research_service.py` (29 tests),
  `backend/tests/unit/test_research_chat.py` (11 tests); LLM, parser, and
  executor mocked, no Ollama/network required
- `backend/core/services/trace_service.py`, unified trace adapter
  (sinks, queue, spans); `docs/plans/next-stage/UNIFIED_LLM_TRACE_PLAN.md`
- `backend/tests/unit/test_trace_service.py` (22 tests, incl. the
  research_stream view path)
- `backend/semantic_search/services/entity_geocoder.py` +
  `query_parser_service.py`, the anchor place-preference and the OBJECT
  back-in vocabulary behind the Belfast grounding failure; see
  `docs/issues/resolved/BELFAST_ANCHOR_SIGNPOST_BUG.md`
