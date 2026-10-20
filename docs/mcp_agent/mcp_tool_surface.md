# MCP Tool Surface: 10 Tools, Two Execution Homes

> **Focus:** how the three query modes (structured JSON, name search, Kuhn's
> template) become MCP tools an agent can call, and how those tools are split
> between the Vite dev server (Node) and the browser (Vue app).
>
> **Key idea:** the MCP server is not a service. It lives inside the Vite
> dev server process, and half of it runs in the browser. Tools are placed by
> what they touch: **data tools** (pure HTTP to Django) run Node-side and
> work headless; **app tools** (Pinia/DOM/devtools) run browser-side because
> the Vue app only exists there. The agent connects over standard MCP
> Streamable HTTP; Node and browser communicate over Vite's HMR WebSocket.

---

## 1. Why This Exists

The frontend's `SemanticSearchPanel.vue` already exposed three independently
testable query modes. The MCP layer makes those same modes callable by an
agent (Goose, or any MCP client), with the user seeing every tool call
rendered on the map. No backend changes were needed for the data tools: they
are thin wrappers over the same endpoints the human UI uses.

---

## 2. Architecture: One Agent, Two Transports

- **Goose agent (Docker)** → JSON-RPC over HTTP (MCP Streamable HTTP) →
  `/__mcp` (`vite-middleware.ts`, `mcp-server.ts` with 10 tools,
  `mcp-handlers.ts`, `server-tools.ts` for Node).
- **Node-side data tools** → `fetch` → Django.
- **Browser (Vue app)** → `channel/server.ts` calls
  `callClient("structuredSearch")` → `server.ws` (HMR WebSocket) →
  `vue-mcp:msg {id, type:"req"}` → `client-runtime.ts` (injected into
  `main.js` by transform) → `channel/client.ts` (`hot.on`) →
  `client-functions.ts` (app tools) → `axios` → Django, or →
  `overlayStore` → map. Reply `{id, type:"res", result}` over the same
  channel.

Mechanics (all from the vendored vue-mcp-server, reused unchanged):
- **Client announcement**: the browser sends `vue-mcp:ping` on load; the
  channel records `lastKnownClient`. App tools fail fast with "no client
  connected" when no tab is open; data tools don't care.
- **Request/response matching**: each call gets a UUID + 60s timeout in a
  pending map (`frontend-v3/src/mcp/channel/server.ts`); the browser replies
  with the same UUID.
- **Type propagation**: `ClientFunctions` (the return type of
  `createClientFunctions`) flows through `CallClient<ClientFns>` → handlers →
  zod schemas. Adding a tool is one function + one handler + one
  registration.

---

## 3. The Tool Surface (10 Tools)

| Tool | Execution home | Backend |
|---|---|---|
| `structuredSearch` | Node (`server-tools.ts`) | `POST /api/nca/semantic-triplet-search/` with `query_tags` |
| `nameSearch` | Node | same endpoint with `natural_query` (romanizer) |
| `templateQuery` | Node | `POST /api/nca/execute-query/` (MapQA parser + executor + enrichment) |
| `getFactorAvailability` | Node | `GET /api/nca/factor-availability/` (G4 coverage) |
| `renderToolOverlay` | Browser (`client-functions.ts`) | `overlayStore` push → map renders |
| `proposeQuery` / `getApprovalState` / `getQueryProposal` | Browser | MapQA HITL modal (`queryProposalStore`) |
| `getInspectorTree` / `getComponentState` | Browser | Vue DevTools introspection |

---

## 4. The Execution-Home Rule

| Tool depends on… | Must run in… | Why |
|---|---|---|
| Pinia store / DOM / devtools (state, rendering, HITL) | **Browser** | The app isn't in Node |
| Only HTTP calls (pure data) | **Node** | No browser round-trip; works headless (`goose run -t`) |

Node-side data tools call Django via `fetch` with the base URL from
`WORLDKG_BACKEND_URL` (default `http://localhost:8000/api`). The human UI
path and the agent path are independent HTTP clients of the same endpoints.
No shared implementation to drift.

---

## 5. The Overlay Layer

`renderToolOverlay` pushes overlay descriptors into the Pinia `overlayStore`
(`frontend-v3/src/stores/overlayStore.js`). `WorldKGMap.vue` subscribes and
renders the spec: `markers` (per-tool colors: blue=structured, red=name,
green=template), `scaled-markers` (score→radius), `radius` (circle +
markers), `markers-line` (anchor polylines). Agent overlays and human
`search-results` stay separate layers so the user can tell who drew what.
(The `/agent` demo page was removed 2026-08-26; agent tool calls are
exercised via the MCP bridge and `mcp-smoke-test.mjs`.)

---

## 6. Key Files

- `frontend-v3/src/mcp/index.ts`, Vite plugin: `transform()` injects the
  client runtime into `main.js`; `configureServer()` mounts `/__mcp`
- `frontend-v3/src/mcp/vite-middleware.ts`, Streamable HTTP transport
  (POST/GET/DELETE, session lifecycle), reused verbatim
- `frontend-v3/src/mcp/mcp-server.ts`, tool registry (zod schemas)
- `frontend-v3/src/mcp/server-tools.ts`, Node-side data tools
- `frontend-v3/src/mcp/mcp-handlers.ts`, browser-path dispatch
- `frontend-v3/src/mcp/client-functions.ts`, browser tool implementations
- `frontend-v3/src/mcp/channel/`, typed RPC over Vite's HMR WebSocket
- `frontend-v3/src/stores/overlayStore.js`, agent overlay state
- `frontend-v3/scripts/mcp-smoke-test.mjs`, MCP wire smoke test (SDK client,
  exercises tools exactly as Goose does)

**Test:** `frontend-v3/scripts/mcp-smoke-test.mjs` against a fresh Vite on
port 5174 (`node scripts/mcp-smoke-test.mjs 5174`).
