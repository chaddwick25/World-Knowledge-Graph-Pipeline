# WorldKG Pipeline and Geo-Spatial Agents

The pipeline transforms heterogeneous, noisy, unstructured OpenStreetMap
data into homogeneous, clean, structured knowledge. It ingests OSM planet
data, builds vector embeddings, aligns entities with Wikidata, and
predicts spatial links — all orchestrated through a multi-stage ETL
The artifacts produced by the pipeline have geo-spatial properties that
are used at the application layer (frontend) for geospatial reasoning.
The geo-spatial agent (or human) accesses these artifacts through a
template-based interface inspired by Werner Kuhn's paper on core concepts
of spatial information. This templating system takes Kuhn's core concepts
and maps them to functional roles used to produce a GeoFlow DAG, 
a directed execution plan that answers geo-spatial based questions. 

```
User types: "Which bars are within 50m of Hollywood Blvd?"

┌──────────────────────────────────────────────────────────┐
│  Step 1: REQUEST                                         │
│  Question enters the system                              │
└──────────────────────────┬───────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────┐
│  Step 2: PARSER                                          │
│  Classifies the question → "find things near a place"    │
└──────────────────────────┬───────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────┐
│  Step 3: TEMPLATE MATCH                                  │
│  Matches the question's classification to a template     │
│  "filter-aggregate-measure" template                     │
└──────────────────────────┬───────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────┐
│  Step 4: EXECUTION                                       │
│  Extracts: what (bars), where (Hollywood Blvd),          │
│            how far (50m)                                 │
│  Template executes against the WorldKG artifacts         │
│  (entity types, spatial relationships, embeddings)       │
│  ← 7 bars within 50m of Hollywood Blvd                   │
└──────────────────────────┬───────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────┐
│  Step 5: RESULTS                                         │
│  Human receives the results — no interpretation          │
└──────────────────────────────────────────────────────────┘
```
<!-- TODO: insert image here -->

### Supported Languages

The romanizer handles cross-script name matching for the following
languages. Script detection is automatic — no language selection needed.
- Korean
- French
- Spanish
- Irish
- German
- Vietnamese
- Portuguese
- Italian
- Scandinavian
- English

### Types of Questions the Templates Handle Well

The templating system succeeds with questions that map to the artifacts
the pipeline produces: entity types (from WorldKG ontology classes),
spatial relationships (from USLP link prediction), semantic similarity
(from GeoVectors embeddings), and cross-script names (from the
romanizer). Below are sample questions grouped by how well the current
templates handle them, with multilingual examples.

```
┌──────────────────────────────┬──────────────────────────────────────────────────────┬──────────────────────────────────────────────────────┐
│ Question type                │ English                                              │ Korean                                               │
├──────────────────────────────┼──────────────────────────────────────────────────────┼──────────────────────────────────────────────────────┤
│ Find things near a place     │ "Which bars are within 50m of Hollywood Blvd?"       │ "할리우드 대로 50m 이내에 어떤 바가 있나요?"                 │
├──────────────────────────────┼──────────────────────────────────────────────────────┼──────────────────────────────────────────────────────┤
│ Find the nearest X           │ "How far is the nearest pharmacy?"                   │ "가장 가까운 약국은 얼마나 멀리 있나요?"                     │
├──────────────────────────────┼──────────────────────────────────────────────────────┼──────────────────────────────────────────────────────┤
│ Compare distances            │ "Which is closer to downtown: the park or library?"  │ "도심에 더 가까운 것은: 공원인가 도서관인가?"              │
├──────────────────────────────┼──────────────────────────────────────────────────────┼──────────────────────────────────────────────────────┤
│ What's around here           │ "What amenities cluster around this train station?"  │ "이 기차역 주변에 어떤 편의시설이 있나요?"                   │
├──────────────────────────────┼──────────────────────────────────────────────────────┼──────────────────────────────────────────────────────┤
│ Cross-script name search     │ "paris bagueete" → 파리바게뜨                           │ "cafe" → 카페                                         │
└──────────────────────────────┴──────────────────────────────────────────────────────┴──────────────────────────────────────────────────────┘


## Accessing the toolset via the GUI

The frontend is a Vue 3 single-page application with a Leaflet map as the
primary visual surface and a tabbed sidebar. Each tab maps to a pipeline
operation and its artifacts. The year selector at the top scopes all
panels to the selected snapshot date.


> **TODO: Add screenshot** — WorldKG map with country eligibility
> highlighting, year selector, and a country selected showing pipeline
> results as overlays.

> **TODO: Add screenshot** — Pipeline progress panel mid-run, showing
> progress bar, current step, and summary stats (entities, aligned, spatial
> links).

> **TODO: Add screenshot** — Query tab with a natural-language question
> typed in, the parsed template name and confidence percentage shown
> below, and the extracted concepts listed with their roles.
>
> **TODO: Add screenshot** — Query tab showing the 3-tier amenity fallback
> trace for a concept resolution.

> **TODO: Add screenshot** — Query confirmation modal showing the editable
> concept slots (amenity, location, radius, object) with Approve / Edit /
> Reject buttons.

> **TODO: Add screenshot** — MapQA results on the map with highlighted
> entities, a popup showing entity metadata, and the execution trace
> showing which factor tables were queried.

> **TODO: Add screenshot** — Metrics tab showing a completed pipeline run
> with all steps listed, their status icons, durations, and the summary
> row (status, steps done, total duration).

> **TODO: Add screenshot** — Augmented data tab showing accepted/rejected
> link counts, acceptance rate, and the map toggle buttons. Include a
> second screenshot with link geometries rendered on the map (green
> accepted, red rejected).

> **TODO: Add screenshot** — Spatial layers tab showing the coverage
> summary (subgraphs, accepted, rejected, rate) and the subgraph cards
> with their availability badges and node/way counts.

> **TODO: Add screenshot** — System summary modal on the overview tab,
> showing planet PBF size, embedding availability, and storage status.

> **TODO: Add screenshot** — Planet init panel with terminal output
> scrolling and the step list showing completed and in-progress steps.

### System Summary

The System Summary modal (accessible from the header) is the operational
dashboard for verifying pipeline prerequisites. It shows the overall
system state across five tabs:

- **Overview** — planet PBF size and availability
- **Embeddings** — GV-Tags / GV-NLE scan status
- **Storage** — database and partition status
- **Paths** — country and subgraph file paths
- **History** — pipeline run history

This is where you confirm that planet initialization completed and all
prerequisites are in place before running a country pipeline.

<!-- TODO: Add screenshot — System summary modal on the overview tab -->

### Query Modes

The Query tab supports three modes that can be tested independently:

**Structured (JSON)** — The user enters OSM tags as JSON (e.g.
`{"amenity": "cafe"}`) or a name in any language (e.g.
`{"name": "파리바게뜨"}`). Uses FastText semantic embeddings + the
romanizing framework for cross-script name matching (Hangul↔Latin,
diacritic stripping for French/Spanish/Irish, etc.). The romanizer
auto-detects the script from the text — no language selection needed.

**Natural language** — The user types a name in any language or script
(e.g. "paris bagueete", "파리바게뜨", "café", "원탕"). The romanizer
activates for cross-script matching (e.g., English "paris bagueete"
matching Korean "파리바게뜨"). FastText provides semantic type matching
as a complementary signal.

**Kuhn's Template** — The user types a full geospatial question (e.g.
"Which bars are within 50m of Hollywood Blvd?"). The parser (TF-IDF +
Naive Bayes) classifies it into one of 9 templates with confidence
scores, and the parsed concepts are displayed for review. The 3-tier
amenity fallback (exact tag → ontology class → FastText semantic) traces
each resolution step so the user sees exactly how a concept like "bar"
was resolved. This mode is completely separate from the other two — it
uses its own parser and executor, and can be tested independently.

---

```


## Core Concepts and Research Foundation

The project's strength comes from two sets of papers under `papers/`. In
rough chronological order, the first set builds the backend pipeline
that generates artifacts from OSM data, and the last two define the
application and view layer that turns those artifacts into
geospatial reasoning.

### Backend — `papers/WorldKG/` (2020–2023)

The WorldKG Project (DFG grant 424985896) produced a sequence of papers
that each contribute a piece of the pipeline. In chronological order:

```
┌──────────────────────────────────────────┬────────────────────────────────────────┬────────────────────────────────────────────────┐
│ Paper                                    │ What it contributes                    │ Pipeline stage                                 │
├──────────────────────────────────────────┼────────────────────────────────────────┼────────────────────────────────────────────────┤
│ Linking OSM with Knowledge Graphs (2020) │ OSM tags → structured KG schemas       │ Step 2 — ontology class enrichment             │
├──────────────────────────────────────────┼────────────────────────────────────────┼────────────────────────────────────────────────┤
│ GeoVectors (2021)                        │ OSM entities → vector embeddings       │ Step 1 — semantic + spatial embeddings         │
├──────────────────────────────────────────┼────────────────────────────────────────┼────────────────────────────────────────────────┤
│ WorldKG: A World-Scale Geographic KG     │ World-scale geographic KG from OSM     │ Step 2 — ontology class system for MapQA       │
│ (2021)                                   │                                        │                                                │
├──────────────────────────────────────────┼────────────────────────────────────────┼────────────────────────────────────────────────┤
│ Attention-Based Vandalism Detection      │ Quality assurance for OSM data         │ (Not a pipeline stage)                         │
│ (2022)                                   │                                        │                                                │
├──────────────────────────────────────────┼────────────────────────────────────────┼────────────────────────────────────────────────┤
│ IGEA — Iterative Geographic Entity       │ OSM entities → Wikidata entries        │ Step 3 — entity alignment                      │
│ Alignment (2023)                         │                                        │                                                │
├──────────────────────────────────────────┼────────────────────────────────────────┼────────────────────────────────────────────────┤
│ USLP — Spatial Link Prediction (2023)    │ Predicts spatial relationships         │ Step 4 — spatial link triplets                 │
└──────────────────────────────────────────┴────────────────────────────────────────┴────────────────────────────────────────────────┘
```

The first half of the pipeline is the generation of artifacts: the
different spatial and semantic tags, the alignment of entities to
Wikidata, and the enrichment of classes. Each WorldKG paper maps to a
pipeline stage that produces a concrete artifact stored in the database.

### Application — `papers/WernerKuhn/`

The last two papers in the flow are used mostly by the application and
view side of the platform:

```
┌──────────────────────────────────────────┬──────────────────────────────────────────────────────┬─────────────────────────────────────────────────────┐
│ Paper                                    │ What it contributes                                  │ Application use                                     │
├──────────────────────────────────────────┼──────────────────────────────────────────────────────┼─────────────────────────────────────────────────────┤
│ Core Concepts of Spatial Information     │ Defines the 6 concepts that geospatial questions     │ The 9 MapQA templates map directly to these         │
│                                          │ ask about (Object, Location, Field, Network, Event,  │ concepts — each template answers a question about   │
│                                          │ Neighbourhood)                                       │ one or two concepts                                 │
├──────────────────────────────────────────┼──────────────────────────────────────────────────────┼─────────────────────────────────────────────────────┤
│ Spatial-Agent (2026)                     │ Shows how an agent can reason over these concepts    │ Blueprint for the planned agent layer — the agent   │
│                                          │ using a GeoFlow Graph                                │ orchestrates the toolset by reasoning in terms of   │
│                                          │                                                      │ Kuhn's concepts                                     │
└──────────────────────────────────────────┴──────────────────────────────────────────────────────┴─────────────────────────────────────────────────────┘
```

The Spatial-Agent paper, published this year (2026), aligns with the
current direction of the platform: the backend pipeline is complete and
stable, and the focus has shifted to the application side — turning the
precomputed artifacts into an agent-accessible toolset for geospatial
reasoning.


---

## Precomputation Strategy

Following Chip Huyen's advice that ML systems should precompute expensive
operations and serve cheap lookups at request time, all spectral,
community, drift, and amenity computations are batch-written to
factor-node tables during the pipeline run. At request time, the GUI
queries these tables via SQL + pgvecto so that a heat kernel diffusion query
becomes a single inner-product lookup, a community summary becomes a
filtered aggregate, and a temporal drift query becomes a join between
snapshot pairs. No graph loading, no eigendecomposition, no SciPy at
request time.

---


## Quickstart

```bash
docker compose -f docker-compose.yml -f compose.override.yml up -d
cd frontend-v3 && npm install && npm run dev
```

For full setup (database extensions, migrations, planet initialization,
romanization, worker configuration, fresh-database resets), see
[docs/SETUP.md](docs/SETUP.md).

---

## Further Reading
 - docs/SETUP.md


## Whats Next ?
 - Baseline tests: Ovid and Drift Tests
 - Add support for more languages
 - CityFM Support
 - Named-entity anchor resolution (unique business names → OSM coordinates)

 ---

## Agent Orchestration

A local agent (Qwen3 14B on Ollama, ~35 tok/s, RTX 4070 Ti Super) runs as
Goose in Docker and orchestrates the MCP toolset at `http://localhost:5173/__mcp`.
The agent reasons in natural language, makes tool calls, and synthesizes
results — no fine-tuning, no vendor function-calling API, just prompt +
tools. Data tools run in the Vite dev server (Node — headless capable);
overlay tools run in the browser so the user SEES every step.

| Tool | Runs in | Purpose |
|---|---|---|
| `structuredSearch` | Node | OSM tag query → entities with scores |
| `nameSearch` | Node | Name query, any language/script (romanizer) → entities |
| `templateQuery` | Node | Full geospatial question → parsed template + answer + enrichment |
| `renderToolOverlay` | Browser | Draw a tool's output on the map (markers / radius / scaled) |
| `proposeQuery` / `getApprovalState` | Browser | HITL approval of agent proposals before execution |

### Worked example — real query against Belize data

The example uses a generic amenity anchor ("cafes" + the city of Belize
City) — anchors that are specific business names (e.g. "Kat's Coffee")
need an anchor-resolution preprocessing step (future work: resolve a
unique name to an OSM entity via `nameSearch`, then pass its coordinates
as the anchor).

```
User: "Which cafes are within 50km of Belize City?"

┌──────────────────────────────────────────────────────────────┐
│  Step 1: TOOL CALL — templateQuery (Node)                    │
│  → {query: "Which cafes are within 50km of Belize City?",    │
│     countryCode: "Belize"}                                   │
│  Parser → FILTER-AGGREGATE-MEASURE (#1)                      │
│    concepts: OBJECT=cafes · AMOUNT=50km · LOCATION=Belize    │
│  Executor → geocode Belize City ✓ → radius filter ✓          │
│  ← 18 entities within 50km, each with distance_m             │
└──────────────────────────┬───────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  Step 2: TOOL CALL — renderToolOverlay (Browser)             │
│  → green markers for the 18 entities + 50km circle around    │
│    Belize City        ← user SEES the radius                 │
└──────────────────────────┬───────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  Step 3: AGENT SYNTHESIS                                     │
│  "18 cafes are within 50km of Belize City. The nearest is    │
│   [name] at [distance] — the map shows the radius and the    │
│   results."                                                  │
└──────────────────────────────────────────────────────────────┘
```

(LLM answer enrichment is live: after the template result, the local model
picks research tools from the toolset — `nameSearch` / `structuredSearch` —
which execute against the same endpoints, then it synthesizes a grounded
enriched answer. Fail-soft: if the model is down, the deterministic answer
is returned unchanged. See `docs/plans/MCP_AGENT_MVP_PLAN.md`.)

Key properties:

- **Tool choice is the agent's** — the LLM picks which tools to call and in
  what order; execution stays deterministic (the same endpoints the human
  UI uses).
- **Every step is visible** — data tools return structured JSON the agent
  reasons over; overlay tools render on the map so the user can verify each
  step before the final synthesis.
- **Fail-soft everywhere** — if the model is down, the deterministic
  answer is returned unchanged.
- **Swap the model, keep the tools** — the tool surface is plain MCP; point
  the same agent at DeepSeek/cloud via `LLM_BASE_URL` and nothing else
  changes. See `docs/plans/MCP_AGENT_MVP_PLAN.md`.

---

## Platform LLM & Local Agent Stack

### Docker services

| Service | Image | Purpose |
|---|---|---|
| `ollama` | `ollama/ollama` | Local LLM server (`qwen3:14b`, `runtime: nvidia`, :11434) |
| `goose` | `ghcr.io/block/goose` | MCP-native agent (on-demand, host networking → localhost reaches Vite + Ollama) |

```bash
docker compose up -d ollama
docker compose exec ollama ollama pull qwen3:14b
docker compose run --rm goose session            # interactive agent TUI
docker compose run --rm goose run -t "Which cafes are within 50km of Belize City?"
```

The goose config lives at `goose/config/config.yaml` (mounted as a directory —
goose rewrites it on session start). The agent connects to the Vite MCP server
at `http://localhost:5173/__mcp` and uses Ollama as its model provider.

### LLMService — provider-abstracted LLM client

`backend/core/services/llm_service.py` talks to Ollama (default, native
`/api/chat` with `think: false` — qwen3's thinking mode otherwise eats the
token budget and returns empty content) or any OpenAI-compatible `/v1`
endpoint. Config:

| Env | Default | Meaning |
|---|---|---|
| `LLM_ENABLED` | `1` | Master switch |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | Endpoint (`http://ollama:11434/v1` in Docker) |
| `LLM_MODEL` | `qwen3:14b` | Model tag |
| `LLM_API_STYLE` | `native` | `native` (Ollama `/api/*`) or `openai` (`/v1/*`) for cloud swap |
| `LLM_TIMEOUT` | `60.0` | Read timeout — cold model reloads take 5–15s |

Every method is fail-soft (`None`/`False`) — callers always fall back to
deterministic paths. Built on `requests` (no SDK — backend is Python 3.8).

### LLM-driven answer enrichment (research loop)

After a `templateQuery` result (e.g. "Found 18 entities within 50km."), the
local model selects 1–2 research tools from the toolset — `nameSearch` /
`structuredSearch` — which execute deterministically against the real
endpoints, then the model synthesizes a grounded enriched answer. If the
model selects nothing, a default action is derived from the result set
(dominant amenity → structuredSearch), so research is forced whenever the
model is up. Implementation: `backend/semantic_search/services/query_enrichment_service.py`.
Verified live on Belize: 18 cafes within 50km of Belize City → enriched with
the top cafe names from a `structuredSearch` research call.

### MVP demo page

`/agent` (`frontend-v3/src/views/AgentPlayground.vue`) — runs the same tools
the agent calls (structured / name / template tabs), logs each step in the
goose format, and renders results on a map via the shared `overlayStore`
(overlays also appear on the main map at `/`). On load it auto-runs the
startup demo query (Belize cafes).

### Tests

`backend/tests/unit/test_llm_service.py` + `test_query_enrichment_service.py`
(50 tests): native/openai client shapes, fail-soft paths, tool-decision
validation, forced-default research, JSON extraction, availability caching.
`frontend-v3/scripts/mcp-smoke-test.mjs` exercises the MCP wire path exactly
as Goose does.

---
 
