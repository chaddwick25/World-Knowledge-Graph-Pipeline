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
 - MCP Agentic Orchestration

 ---

## Agent Orchestration

A local agent (Qwen 2.5 14B, ~35 tok/s) orchestrates the toolset
above. The agent reasons in natural language, makes tool calls, and
synthesizes results — no fine-tuning, no function-calling API, just
prompt + tools. The same GeoFlow DAG engine runs under the hood; the
agent adds reasoning before and interpretation after.

```
User types: "Find Paris Baguette in Korea and tell me what's nearby"

┌──────────────────────────────────────────────────────────┐
│  Step 1: REQUEST                                         │
│  Agent reads the question                                │
└──────────────────────────┬───────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────┐
│  Step 2: AGENT REASONING                                 │
│  "I need to find a place by name, then find what's       │
│   nearby."                                               │
└──────────────────────────┬───────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────┐
│  Step 3: TOOL CALL — nameSearch                          │
│  → "paris bagueete" in Korea                             │
│  ← Paris Baguette (파리바게뜨) at 37.5°N, 127.0°E       │
└──────────────────────────┬───────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────┐
│  Step 4: AGENT REASONING                                 │
│  "Found it. Now I'll ask what's within 500m."            │
└──────────────────────────┬───────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────┐
│  Step 5: GEOFLOW DAG                                     │
│  → "Which amenities are within 500m of 37.5, 127.0?"     │
│  Parser classifies → template match → execution          │
│  against WorldKG artifacts                               │
│  ← 7 amenities within 500m                               │
└──────────────────────────┬───────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────┐
│  Step 6: AGENT SYNTHESIS                                 │
│  "Paris Baguette is in Seoul. Within 500m there are      │
│   3 cafes, 1 pharmacy, 2 bus stops, and 1 park."         │
└──────────────────────────────────────────────────────────┘
```

---
 
