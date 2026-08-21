# WorldKG Pipeline

Productizing open-source models for geospatial reasoning via the [WorldKG Project](https://www.vgiscience.org/projects/worldkg.html).

The pipeline transforms heterogeneous, noisy, unstructured OpenStreetMap data into homogeneous, clean, structured knowledge. It ingests OSM planet data, builds vector embeddings, aligns entities with Wikidata, and predicts spatial links — all orchestrated as a multi-stage ETL pipeline driven by a [Celery](https://docs.celeryq.dev/) canvas.

The artifacts produced will be consumed by agents for geospatial reasoning. The current architectural direction is MapQA — a parser → executor → HITL pipeline that maps natural-language questions to 5 template-specific execution functions. The parser, executor, PostGIS spatial search, FastText fallback, MCP/HITL frontend, and test suite are all implemented.

---

## WorldKG Project

The WorldKG Project (DFG grant 424985896, 2019–2023) addressed the semantic
gap between OpenStreetMap and knowledge graphs. This pipeline productizes
that research into a running system that:

1. **Closes the OSM↔Wikidata gap** — IGEA entity alignment links OSM
   entities to Wikidata knowledge graph entries using cross-attention over
   semantic and geo-spatial features.
2. **Predicts spatial relationships** — USLP discovers spatial links between
   entities using tri-space scoring (geographic + name + class similarity),
   producing ground-truth-style triplets.
3. **Learns graph representations** — GeoVectors embeddings (GV-Tags 300D
   semantic + GV-NLE 100D spatial) and DeepWalk random-walk embeddings
   capture entity similarity on the k-NN graph manifold.

---

## Artifacts and Application Functionality

The pipeline produces artifacts that the GUI application layer consumes at
runtime. Each pipeline operation creates data that flows into a specific
frontend component, so the user sees the results of batch computation
without any heavy processing at request time.

The graph spectral analysis and temporal drift operations are where the
pipeline leverages the WorldKG Project's artifacts to deliver the
geospatial reasoning capabilities described in Werner Kuhn's core concepts
of spatial information and the Spatial-Agent framework. The spectral
eigenbasis, community structure, and heat kernel diffusion provide the
computational substrate for Kuhn's **Network** concept (connectivity,
diffusion spread), **Event** concept (change detection via temporal drift),
and **Field** concept (continuous attribute propagation via the Laplacian).
These are materialized as factor-node tables that the GUI queries at
request time through the MapQA templating system — no graph loading, no
eigendecomposition, no SciPy at request time. Just SQL + pgvector lookups
against precomputed results.

### How Pipeline Operations Connect to the GUI

The frontend is a Vue 3 single-page application with a Leaflet map as the
primary visual surface and a tabbed sidebar. Each tab maps to a pipeline
operation and its artifacts:

**WorldKG Map** — The central map renders country eligibility (supported
countries are clickable, unsupported are transparent), pipeline results as
map overlays, and MapQA query results as highlighted entities. The year
selector at the top scopes all panels to the selected snapshot date. This
is the canvas that all other panels paint onto — USLP link geometries,
MapQA result highlights, and subgraph boundaries all render here.

> **TODO: Add screenshot** — WorldKG map with country eligibility
> highlighting, year selector, and a country selected showing pipeline
> results as overlays.

**Pipeline Progress Panel** — This is the pipeline control surface. It
shows the current run status (idle / running / completed / failed), a
progress bar, the active step name, and a summary of results so far (total
entities, aligned entities, spatial links). The Run Pipeline and Cancel
buttons live here. During a run, WebSocket updates flow in real time —
this is where the user watches Steps 1–6, 5c, and 5d execute and sees
artifacts being produced.

> **TODO: Add screenshot** — Pipeline progress panel mid-run, showing
> progress bar, current step, and summary stats (entities, aligned, spatial
> links).

**Query Tab** — The Semantic Search Panel is the entry point for both
semantic triplet search and natural-language MapQA queries. The embeddings
produced by Step 1 (GeoVectors) and the enrichment from Step 2 (WorldKG
ontology classes) power this panel. A SubdivisionSelector lets the user
filter by administrative subdivision using Wikidata QIDs. In NL mode, the
user types a question, the parser classifies it into one of 9 templates
with confidence scores, and the parsed concepts are displayed for review.
The 3-tier amenity fallback (exact tag → ontology class → FastText
semantic) traces each resolution step so the user sees exactly how a
concept like "bar" was resolved.

> **TODO: Add screenshot** — Query tab with a natural-language question
> typed in, the parsed template name and confidence percentage shown
> below, and the extracted concepts listed with their roles.
>
> **TODO: Add screenshot** — Query tab showing the 3-tier amenity fallback
> trace for a concept resolution.

**Query Confirmation Modal (HITL)** — The parsed query is presented as an
editable confirmation modal via an MCP bridge. The user can approve, edit
concept slots, or reject. Only approved queries proceed to execution. This
is the transparency layer — the user sees exactly what the parser
understood before any computation runs.

> **TODO: Add screenshot** — Query confirmation modal showing the editable
> concept slots (amenity, location, radius, object) with Approve / Edit /
> Reject buttons.

**MapQA Results on Map** — After the executor resolves the approved query,
results are rendered on the map as highlighted entities with popups showing
entity metadata. This is where the spectral analysis (Step 5c) and temporal
drift (Step 5d) artifacts become visible to the user — when a SPECTRAL-
ANALYSIS, COMMUNITY-DETECT, TEMPORAL-DRIFT, or EVENT-DIFFUSION template is
executed, the results come from precomputed factor tables resolved via
pgvector at request time. No graph loading, no eigendecomposition at query
time — just SQL lookups against batch-written results.

> **TODO: Add screenshot** — MapQA results on the map with highlighted
> entities, a popup showing entity metadata, and the execution trace
> showing which factor tables were queried.

**Metrics Tab** — The Pipeline Metrics Panel shows the per-step performance
timeline for the latest pipeline run. Each pipeline stage (Steps 1–6, 5c,
5d) is listed with its status icon (pending / running / completed / failed),
duration, and any step messages. The summary shows overall status, steps
completed, and total duration. This is the post-run operational view — the
user can see which steps succeeded, how long each took, and where failures
occurred.

> **TODO: Add screenshot** — Metrics tab showing a completed pipeline run
> with all steps listed, their status icons, durations, and the summary
> row (status, steps done, total duration).

**Augmented Data Tab** — The Augmented Data Panel is the USLP visualization
surface. The spatial link triplets produced by Step 4 become explorable
here: accepted and rejected link counts, acceptance rate, total entities,
and map toggles that render accepted and rejected link geometries directly
on the WorldKG Map. The user can toggle accepted links (green) and rejected
links (red) on the map to visually inspect where USLP predicted spatial
relationships and how confident it was.

> **TODO: Add screenshot** — Augmented data tab showing accepted/rejected
> link counts, acceptance rate, and the map toggle buttons. Include a
> second screenshot with link geometries rendered on the map (green
> accepted, red rejected).

**Spatial Layers Tab** — The Spatial Metrics Panel shows the subgraph
profile inventory for the selected country. Each subgraph is displayed as a
card with its name, slug, PBF/Poly/Pickle availability badges, node and way
counts, and metadata status. A coverage summary shows total subgraphs,
accepted and rejected entity counts, and the acceptance rate. This is
where the user sees how a country has been split into administrative
subdivisions for parallel processing — the same subdivision structure that
drives the Step 5c spectral routing for large countries (≥ 5M nodes).

> **TODO: Add screenshot** — Spatial layers tab showing the coverage
> summary (subgraphs, accepted, rejected, rate) and the subgraph cards
> with their availability badges and node/way counts.

**System Summary Modal** — A modal accessible from the header that shows
the overall system state across five tabs: overview (planet PBF size and
availability), embeddings (GV-Tags / GV-NLE scan status), storage (database
and partition status), paths (country and subgraph file paths), and history
(pipeline run history). This is the operational dashboard for verifying
that planet initialization completed and that all prerequisites are in
place before running a country pipeline.

> **TODO: Add screenshot** — System summary modal on the overview tab,
> showing planet PBF size, embedding availability, and storage status.

**Planet Init Panel** — The PlanetInitPanel shows the status of the
`init_planet` command with a live terminal output feed. Each of the 14
initialization steps is listed with its status. This is the operational
view for the one-time planet setup — the user watches hierarchy resolution,
embedding scans, ontology enrichment, and boundary generation complete in
real time.

> **TODO: Add screenshot** — Planet init panel with terminal output
> scrolling and the step list showing completed and in-progress steps.

### Precomputation Strategy

The key design decision is that all expensive computation happens during
the pipeline run, not at request time. The spectral eigendecomposition,
community detection, drift computation, and amenity embeddings are all
batch-written to factor-node tables during Steps 5c, 5d, and
`init_planet`. At request time, the GUI queries these tables via SQL +
pgvector — a heat kernel diffusion query becomes a single inner-product
lookup, a community summary becomes a filtered aggregate, and a temporal
drift query becomes a join between snapshot pairs.

For large countries (≥ 5M nodes), the spectral solve is split across
subdivisions, with functional-map transport matrices bridging adjacent
subgraph eigenbases. This keeps each solve small enough to fit on a single
GPU while preserving cross-subgraph diffusion at runtime. The routing is
automatic — small countries get one global solve, large countries get
subdivision-scoped solves with transport.

---

## MapQA — The Templating System

MapQA (Map Question Answering) is the application's reasoning layer. It
maps natural-language geospatial questions to structured execution plans
through a cohesive three-stage pipeline grounded in Kuhn's core concepts
of spatial information and the Spatial-Agent framework's GeoFlow Graph
formalism.

### Stage 1: Parser (NL → Structured Plan)

A TF-IDF + MultinomialNB classifier assigns the question to one of 9
templates. A one-vs-rest Logistic Regression concept extractor pulls out
amenity, location, radius, and open-vocabulary OBJECT concepts. A Logistic
Regression role assigner maps concepts to DAG roles (SUB_COND → COND →
SUPPORT → MEASURE) following the precedence grammar. The output is a
GeoFlow DAG — a directed acyclic graph where each node is a spatial concept
transformation and each edge is a data dependency, directly mirroring the
concept transformation formalism from the Spatial-Agent paper.

### Stage 2: Executor (Plan → Answer)

The executor walks the DAG in topological order and dispatches to
template-specific execution functions. Each function uses the factor-table
path as authoritative:

- **Spectral/diffusion/community queries** resolve via SQL + pgvector `<#>`
  against `factor_*` tables — no graph loading at request time.
- **Distance/radius queries** fall back to PostGIS `ST_DWithin` and
  `Distance` with GiST KNN operators.
- **Amenity resolution** uses the 3-tier fallback (exact tag → ontology
  class → FastText semantic), checking `factor_amenity_embedding` first.
- **Cross-subgraph diffusion** loads transport matrices from
  `factor_subgraph_transport` and transports eigen-loadings across
  subgraph boundaries via matrix multiplication.

### Stage 3: HITL (Human-in-the-Loop Confirmation)

The parsed query is presented to the user as an editable confirmation modal
via an MCP (Model Context Protocol) bridge. The user can approve, edit
concept slots, or reject. Only approved queries proceed to execution. This
ensures the parser's interpretation is transparent and correctable before
any computation runs.

### Template Coverage

The 9 templates map directly to Kuhn's core concepts and the Spatial-Agent
paper's operator categories:

| Template | Core Concept | Execution Path | Example |
|----------|-------------|---------------|---------|
| FILTER-AGGREGATE-MEASURE | Object, Location | PostGIS `ST_DWithin` + `Distance` | "How many schools within 2km of this hospital?" |
| GEOCODE-BATCH-COMPARE | Object, Neighbourhood | PostGIS distance ordering | "Which is closer to downtown: the park or the library?" |
| PLACE-ATTRIBUTE-QUERY | Object, Field | Factor tables (spectral/community) + PostGIS | "What amenities cluster around this train station?" |
| LOCATION-BEARING-CLASSIFY | Location, Neighbourhood | PostGIS `ST_DWithin` + bearing calc | "What's north of the river?" |
| OBJECT-FIELD-MEASURE | Object, Field | PostGIS `ST_DWithin` (optional) | "How far is the nearest pharmacy?" |
| SPECTRAL-ANALYSIS | Network | Factor tables (eigenvalues, λ₂, gap) | "What's the spectral structure of this region?" |
| TEMPORAL-DRIFT | Event | Factor tables (drift metrics) | "How has this area changed between snapshots?" |
| COMMUNITY-DETECT | Object, Network | Factor tables (Louvain communities) | "What communities exist in this district?" |
| EVENT-DIFFUSION | Network, Event | Factor tables (heat kernel via pgvector) | "How would an event at this location spread?" |

### Geographic Scoring

Both the MapQA executor and the semantic search view use the USLP geographic
space score from Mann et al. 2023 §3.3. The formula encodes anchor and
candidate coordinates to geohash at P4 precision (~39km cells), computes
haversine distance between cluster centers, and normalizes by d_max:

```
geo_score = 1 - d_cluster / d_max
```

This replaces the old `1/(1+d_km)` decay, which treated all distances
uniformly. The USLP formula is scale-aware — entities in the same geohash
cell score 1.0, and scores decay linearly to 0.0 at d_max.

Template-specific behavior:
- **FILTER-AGGREGATE-MEASURE** with an explicit radius uses raw haversine
  with the user's radius as d_max (no geohash quantization — the user gave
  an exact distance).
- **OBJECT-FIELD-MEASURE** returns 0.0 (distance IS the answer, not a
  ranking signal).
- All other templates use P4 geohash with d_max = 39km (P4 cell width).

### USLP Signal Boost

When the USLP pipeline has predicted spatial links for the anchor entity
(`SpatialTripletScore.predicted=True`, normalized score >= 0.7), entities
that appear as predicted link tails receive a +0.5 score boost. This
connects the link prediction layer to search ranking -- if USLP predicts
that the anchor has a spatial relationship with a candidate, that
candidate gets a small ranking advantage. The boost is weighted low
because USLP relations (isInCounty, addrSuburb) don't directly map to
proximity queries like "find bars near a bus station."

### Radius Query Routing

Queries containing explicit radius language (`within Xkm of`, `within Xm
of`) are deterministically routed to `FILTER-AGGREGATE-MEASURE` regardless
of the TF-IDF classifier's prediction. This prevents radius-bearing
queries from being misrouted to `PLACE-ATTRIBUTE-QUERY`, which uses heat
kernel diffusion -- a graph connectivity measure that does not enforce
geographic distance. A safety guard in the `PLACE-ATTRIBUTE-QUERY`
executor also filters heat kernel results by haversine distance when an
AMOUNT concept is present, so even if the parser misclassifies a radius
query, geographically distant entities are excluded.

### Tag Filtering

The semantic search view filters the queryset to entities that have the
queried tag keys present (`tags__has_key`), even when exact tag matching
is disabled. This prevents entities without the tag from polluting
results -- querying `{"cuisine": "jamaican"}` no longer returns cafes
with no cuisine tag. A tag match boost of +1.0 is added to the final
score for each query tag value that exactly matches the entity's tag
value, ensuring `cuisine=jamaican` ranks above `cuisine=indian` even
when their FastText embeddings are semantically similar.

---

## Planet Initialization

A single `init_planet` management command runs once as a Docker entrypoint
step after migrations. It is idempotent (gated by a `PlanetSnapshot` soft
lock) and initializes: planet/continent PBFs, OSM-Wikidata hierarchy,
country paths, subgraph profiles, Wikidata IDs, embedding scans, WorldKG
ontology, and OSM boundaries. The backend container runs it automatically on
startup; the worker skips it.

---

## Quickstart

```bash
# Build images
docker compose -f docker-compose.yml -f compose.override.yml build

# Start databases and Redis
docker compose -f docker-compose.yml -f compose.override.yml up -d postgres-default postgres-vectors redis

# Start backend API + Celery worker
docker compose -f docker-compose.yml -f compose.override.yml up -d backend worker

# Start frontend
cd frontend-v3
npx vite --port 5173 --host 0.0.0.0
```

Worker configuration:
- `--pool=prefork --concurrency=4` — 4 CPU workers for parallel upserts/IGEA/USLP
- GPU tasks use per-GPU slot locks — all subgraphs go to cuda:0 with
  concurrency=1 by default. Configure via `GV_NLE_GPU_DEVICES` and
  `GV_NLE_GPU_CONCURRENCY` env vars.
- `RUN_MIGRATIONS` env var: backend runs migrations, worker waits for them
- `RUN_INIT_PLANET` env var: backend runs `init_planet` after migrations,
  worker skips it

---

## Fresh Database Setup

After dropping and recreating both databases:

```bash
# 1. Migrate both databases
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py migrate
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py migrate --database=vectors

# 2. Initialize planet data (hierarchy, profiles, paths, embeddings, ontology, boundaries)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py init_planet
```

The backend container's entrypoint runs `init_planet` automatically on
startup (`RUN_INIT_PLANET=true`), so simply restarting the backend after a
DB reset will trigger it. The worker has `RUN_INIT_PLANET=false`.

On a fresh vectors DB, `semantic_search_osmentity` starts as a monolith
table. The first country pipeline run's Step 1 detects the empty monolith,
converts it to a partitioned table (`PARTITION BY LIST (snapshot_id)` →
`LIST (country_code)`), and creates the per-country leaf partition
automatically — no manual cutover needed.
