# Preserving the Spatial-Agent Model Architecture

How the agentic geospatial reasoning methodology from the [Spatial-Agent: Agentic Geo-spatial Reasoning with Scientific Core Concepts](https://papers/WernerKuhn/Spatial-Agent:%20Agentic%20Geo-spatial%20Reasoning%20with%20Scientific%20Core%20Concepts.pdf) research paper (Kuhn et al.) was integrated into the Django + Vue 3 pipeline.

---

## 1. The MCP Tool Surface

### The Original Implementation
The Spatial-Agent paper describes an agent that reasons over geospatial data by selecting and chaining tools. The agent receives a natural-language question, decomposes it using Kuhn's core concepts, selects appropriate tools from a palette, executes them, and synthesizes an answer from the results. The paper frames this as an agentic loop where the LLM acts as a controller selecting tools based on the question's semantic content.

### The Updated Implementation
The pipeline externalizes the agent's tool palette as an MCP (Model Context Protocol) server, exposing 9 tools that an external agent (Goose in Docker, qwen3:8b on Ollama) can call and chain. The MCP server lives in the frontend Vue 3 project and bridges Node-side data tools with browser-side application tools. The standalone `nameSearch` tool was removed 2026-09-28: its cross-script name lookup is reachable through `structuredSearch` with a `name` tag (same romanizer-backed path), and phrase-level questions belong to `templateQuery`.

**Design Rule:** Data tools run in Node (headless-capable, direct fetch to Django). Application tools run in the browser (the Vue app only exists there; Pinia/DOM/devtools cannot be touched from Node).

**The 9 MCP Tools:**

| Tool | Execution Home | Purpose |
|------|---------------|---------|
| `structuredSearch` | Node (`server-tools.ts`) | OSM tag query → entities with scores; a `name` tag routes to the romanizer-backed fuzzy name path |
| `templateQuery` | Node | Full geospatial question → parsed + executed + enriched answer |
| `getFactorAvailability` | Node | G4 coverage booleans per factor table |
| `proposeQuery` | Browser (`client-functions.ts`) | Push parsed query to HITL modal |
| `getApprovalState` | Browser | Poll HITL approval state |
| `getQueryProposal` | Browser | Get current proposal (with user edits) |
| `renderToolOverlay` | Browser | Draw tool output on map (markers/radius/scaled) |
| `getInspectorTree` | Browser | Vue DevTools component tree introspection |
| `getComponentState` | Browser | Vue component state introspection |

**Code Reference:** `frontend-v3/src/mcp/mcp-server.ts` (tool registration), `frontend-v3/src/mcp/server-tools.ts` (Node-side data tools, lines 66-150), `frontend-v3/src/mcp/mcp-handlers.ts` (browser bridge), `frontend-v3/src/mcp/client-functions.ts` (client-side calls).

---

## 2. Tool Chaining and Cross-Language Matching

### The Original Implementation
The Spatial-Agent paper demonstrates tool chaining: the agent receives a user question in one language, looks up an entity by name, then feeds the coordinates into a spatial query. The paper highlights cross-language matching as a key capability.

### The Updated Implementation
The pipeline makes cross-language tool chaining transparent via a romanizer that normalizes scripts before embedding lookup. The agent can receive a user question in English about a Korean entity, look it up by name via `structuredSearch` with `queryTags: {"name": "파리바게뜨"}` (which routes to the romanizer-backed fuzzy name path and romanizes Hangul to Latin), then feed the coordinates into a `templateQuery` radius question.

* **Mechanism:** `structuredSearch` in `server-tools.ts` sends the query to Django's `/api/nca/semantic-triplet-search/` endpoint with `query_tags`. A `name` key derives the name search term (`search.py:177-179`), the backend romanizer (`backend/semantic_search/services/romanizer_service.py`) normalizes the script, then n-gram Jaccard scores the cross-script match with FastText as a complementary signal.
* **Transformation:** The agent chains `structuredSearch` (name tag) → `templateQuery` → `renderToolOverlay` without needing to know the script or language of the intermediate results.
* **Romanizer Scope:** Phonetic scripts only (Hangul, diacritic Latin, identity). Chinese Hanzi and Japanese Kanji are intentionally excluded.

---

## 3. Per-Tool Map Visualization

### The Original Implementation
The Spatial-Agent paper visualizes agent tool outputs on a map to provide spatial context for the reasoning loop.

### The Updated Implementation
The pipeline renders each tool's output as a dedicated map overlay via `renderToolOverlay`, which pushes into the Pinia `overlayStore`. `WorldKGMap.vue` subscribes and renders `agentOverlayLayer`.

* **Color Coding:** blue = `structuredSearch`, green = `templateQuery`.
* **Overlay Types:** `scaled-markers` (sized by score), `radius` (circle), `markers-line` (connected points).
* **Accumulation:** Overlays accumulate as the agent chains tools, providing a visual trace of the reasoning path.

**Code Reference:** `frontend-v3/src/stores/overlayStore.js`
```js
const id = overlay.id || `ov-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
overlays.value.push({ id, ...overlay })
return { status: 'rendered', id, overlayCount: overlays.value.length }
```

---

## 4. Human-in-the-Loop (HITL) Confirmation

### The Original Implementation
The Spatial-Agent paper introduces a human-in-the-loop step where the user reviews the agent's parsed query before execution. This prevents the agent from executing a misclassified query without user consent.

### The Updated Implementation
The pipeline implements HITL as a Pinia-driven Vue 3 modal with a state machine that the MCP server can drive programmatically.

**State Machine:**

| State | Description |
|-------|-------------|
| `idle` | No proposal pending |
| `pending` | Proposal pushed, awaiting user action |
| `approved` | User approved without edits |
| `edited` | User edited concept slots then approved |
| `rejected` | User rejected the proposal |

**Flow:**
1. Agent (or MCP client) calls `proposeQuery` → modal opens with `proposedQuery.template`, editable concepts, confidence score.
2. User clicks `Approve`, `Save Edits & Approve`, or `Reject`.
3. Agent polls `getApprovalState` until state transitions from `pending`.
4. If `edited`, agent calls `getQueryProposal` to retrieve the user-edited version.

**Code Reference:**
- `frontend-v3/src/components/QueryConfirmationModal.vue` (lines 1-368): renders the modal with editable concept slots and confidence.
- `frontend-v3/src/stores/queryProposalStore.js` (lines 19-64): state machine and actions.
- `frontend-v3/src/mcp/client-functions.ts` (lines 72-94): MCP bridge exposing `proposeQuery`, `getApprovalState`, `getQueryProposal`.

---

## 5. Agent Infrastructure

### The Original Implementation
The Spatial-Agent paper describes an agent running in a containerized environment with access to an LLM for reasoning and tool selection.

### The Updated Implementation
The pipeline does not contain an in-repo agent loop. The agent is externalized to a Docker container running Goose with a local Ollama LLM. The MCP server is the bridge.

**Configuration (from `.devin/rules/02-backend.md`):**
- **Agent:** Goose in Docker
- **LLM:** `qwen3:8b` on Ollama (`LLM_BASE_URL=http://ollama:11434/v1`)
- **GPU:** RTX 4070 Ti Super 16GB, shared with pipeline compute via `NVIDIA_VISIBLE_DEVICES=1` + `OLLAMA_KEEP_ALIVE=-1` + `OLLAMA_NUM_PARALLEL=6` (qwen3:8b resident 8.7 GiB, load-tested ceiling 2026-09-16)
- **Smoke Test:** `frontend-v3/scripts/mcp-smoke-test.mjs` exercises the MCP tools exactly as Goose would, using the MCP SDK client against `/__mcp`.

**LLM Service (backend):**
- `backend/core/services/llm_service.py`
- Default model: `_DEFAULT_MODEL = "qwen3:8b"` (line 50)
- Default base URL: `http://localhost:11434/v1` (line 49)
- Used by `QueryEnrichmentService` for answer synthesis (direct path: 1 call; agent path: tool-selection loop + synthesis)

---

## 6. SSE Streaming for Agent Responses

### The Original Implementation
The Spatial-Agent paper streams the agent's reasoning and answer to the user as it progresses, providing real-time feedback during tool selection and execution.

### The Updated Implementation
The pipeline streams parsed query, execution results, context, and LLM answer tokens via Server-Sent Events.

**Event Sequence:**

| Path | Events |
|------|--------|
| **Direct** | `parsed` → `executed` → `context` → `answer_delta` (token stream) → `done` |
| **Agent** | `parsed` → `executed` → `research` → `research_out` → `answer_delta` → `done` |

* **Mechanism:** `execute_query_stream` view in `backend/worldkg_nca/views/search.py` (lines 1111-1191). Plain Django view (`@require_GET`), because DRF content-negotiation rejects `Accept: text/event-stream`. Pipeline runs in a worker thread; events flow through `queue.Queue` to `StreamingHttpResponse`.
* **Frontend:** SSE stream consumed with `EventSource` (GET-only). URL built from `axios.defaults.baseURL` (relative path hits Vite with no proxy).

---

## 7. Direct Path vs Agent Path Enrichment

### The Original Implementation
The Spatial-Agent paper's enrichment loop has the LLM select tools (`nameSearch`, `structuredSearch`), execute them, and synthesize an answer from the results. This requires multiple LLM calls (tool selection + synthesis).

### The Updated Implementation
The pipeline implements two enrichment paths, reducing LLM calls on the direct path from 3 to 1:

**Direct Path (default, 1 LLM call):**
1. `EntityContextService.get_context(osm_ids, country_code, snapshot_date, template)` fetches deterministic context via 3 indexed SQL queries: USLP spatial links, factor-table communities, class distribution (~50ms-1.5s).
2. `QueryEnrichmentService.synthesize(question, template, concepts, results, context, ...)` makes one LLM call grounded in the context digest (`_format_context_for_prompt`, ~145 tokens).
3. Fail-soft: any LLM failure keeps the deterministic answer.

**Agent Path (MCP agents, retains original loop):**
1. `QueryEnrichmentService.enrich(question, template, concepts, results, ...)` uses the original research loop: LLM selects `nameSearch`/`structuredSearch` via `chat_json`, executes them, then synthesizes.
2. This path is for MCP agent orchestration where an external agent chains tool calls.

**Enriched-Answer Cache:**
- TTL cache (`LLM_ANSWER_CACHE_TTL_SECONDS`, default 1800s, in-process) keyed by `md5("v2|{namespace}|{question}|{template}|{country}|{snapshot}")`.
- Namespaced per method (`enrich` vs `synthesize`) so the two paths never share entries.
- Cache hits replay only `answer_delta` events; the `context` event is always re-fetched fresh.
- Repeated queries: ~12s → ~1-2.5s. Cleared on dev-server reload.

**Paper vs Code:** The direct path replaces the paper's research-tool selection loop with deterministic context fetches, reducing latency and LLM cost. The agent path retains the original pattern for MCP agents that need the tool-selection loop.

---

## 8. MCP Transport and Smoke Testing

### The Original Implementation
The Spatial-Agent paper does not specify a transport protocol for the agent-tool interface.

### The Updated Implementation
The pipeline uses Streamable HTTP transport (POST/GET/DELETE on `/__mcp`) via the `vue-mcp-server` Vite plugin.

* **Server:** `frontend-v3/src/mcp/index.ts` (Vite plugin: injects client runtime, mounts `/__mcp` middleware).
* **Transport:** `frontend-v3/src/mcp/vite-middleware.ts` (Streamable HTTP).
* **Smoke Test:** `frontend-v3/scripts/mcp-smoke-test.mjs` uses the MCP SDK client to connect to `/__mcp` and exercise all tools exactly as Goose would. Run with `node frontend-v3/scripts/mcp-smoke-test.mjs`.

---

## 9. Removed Components

The embedded agent MVP demo page (`AgentPlayground.vue` at `/agent`) was removed 2026-08-26. Agent tool calls are now exercised via the `vue-mcp-server` bridge and `mcp-smoke-test.mjs`, not an embedded page. The following were deleted:
- `AgentPlayground.vue` (route `/agent`)
- `useTemplateQueryStream.js`
- `agentQueryStore.js`
- `agentMode` prop on `SemanticSearchPanel.vue`
- `hideSelectedBorder` prop on `WorldKGMap.vue`
- Agent/Human toggle on `Home.vue`

The app is human-toolset-only in the UI; agent/MCP interaction happens via the bridge.

---

## 10. Cross-References

| Topic | Document |
|-------|----------|
| MapQA parser and executor (template classification, concept extraction) | `MAPQA.md` |
| Kuhn core concepts mapping (paper vs code) | `KUHN_CORE_CONCEPTS.md` |
| MCP tool surface details | `docs/Schematics/08_Agent_MCP_LLM/01_MCP_Tool_Surface.md` |
| Platform LLM enrichment | `docs/Schematics/08_Agent_MCP_LLM/02_Platform_LLM_Enrichment.md` |
| SSE streaming performance | `docs/Schematics/08_Agent_MCP_LLM/03_SSE_Streaming_Performance.md` |
| Docker agent stack | `docs/Schematics/08_Agent_MCP_LLM/04_Docker_Agent_Stack.md` |
| Query modes (structured, natural, template) | `.devin/rules/05-query-modes.md` §5.6 |
