# External Concepts for Result Visualization & MCP Tooling

> **Focus:** core concepts from two Spatial Dev Guru posts, applied to
> **visualizing MapQA executor results** in two versions: **V1
> deterministic** (template + result shape → fixed viz) and **V2
> agent-controlled** (agent selects from a viz palette via MCP).
>
> **Source posts:**
> 1. [Building an OpenStreetMap Renderer from Scratch](https://spatial-dev.guru/2026/01/11/building-an-openstreetmap-renderer-from-scratch/), Web Mercator, tile pyramids, world↔pixel math.
> 2. [Spatial Interpolation in Python](https://spatial-dev.guru/posts/page/2/), IDW, kernel/RBF interpolation, continuous surfaces from scattered samples.
>
> **Assumption:** the DAG template configuration is complete and tested
> end-to-end. The parser produces a GeoFlow DAG, the executor runs it against
> PostGIS + pgvector, and returns `{template, results, answer, trace,
> latency_ms}`. This document picks up at the next step: **what to do with
> the result, visually, in the application layer.**

---

## 1. Project Context: What the Executor Returns

The executor (`backend/semantic_search/services/query_executor_service/`)
returns a dict with five keys:

```python
{
    "template": "FILTER-AGGREGATE-MEASURE (#1)",
    "results": [ {osm_entity_dict}, ... ],   # list, possibly empty
    "answer": "Found 3 bars within 50m of Hollywood Blvd.",
    "trace": [ {step, input, output, ...}, ... ],  # ordered execution trace
    "latency_ms": 142.3,
}
```

The `trace` is the ordered list of operations the executor walked (geocode,
tag match, ontology class resolution, FastText semantic fallback, distance
ranking, augmented enrichment). Each trace step has structured fields. The
`results` list contains OSM entity dicts with geometry, tags, distance (when
relevant), and enrichment metadata.

The 5 templates produce structurally distinct result shapes:

| # | Template | Result shape | Spatial character |
|---|---|---|---|
| 1 | FILTER-AGGREGATE-MEASURE | N entities within radius R of anchor point | cluster around a point |
| 2 | OBJECT-FIELD-MEASURE | 1 entity with attribute/field value | single point |
| 4 | GEOCODE-BATCH-COMPARE | N entities ranked by distance to anchor | fan-in to a point |
| 5 | LOCATION-BEARING-CLASSIFY | N entities in a bearing sector from anchor | directional wedge |
| 8 | PLACE-ATTRIBUTE-QUERY | 1 entity with attribute list | single point |

This `(template, result_shape)` pair is the natural key for deterministic
visualization selection (V1) and the natural input for an agent to reason
about (V2).

---

## 2. Current MCP Surface (What Exists Today)

The dev-only MCP bridge at `/__mcp` (`frontend-v3/src/mcp/`) exposes **10
tools**:

| MCP Tool | Purpose |
|---|---|
| `getInspectorTree` | Vue component tree (markdown tree) |
| `getComponentState` | State of one Vue component |
| `proposeQuery` | Push a parsed query to the frontend for HITL approval |
| `getApprovalState` | Poll approval state (idle/pending/approved/edited/rejected) |
| `getQueryProposal` | Full proposal state including execution result if any |
| `structuredSearch` | OSM tag query → entities with scores (Node-side); a `name` tag routes to the romanizer-backed fuzzy name path |
| `templateQuery` | Full geospatial question → parsed + answer + enrichment (Node-side) |
| `renderToolOverlay` | Draw tool output on the map (markers/radius/scaled) (browser) |
| `getFactorAvailability` | G4 coverage booleans per factor table |

The HITL modal (`QueryConfirmationModal.vue`) renders the parsed query as
editable concept slots with Approve / Edit / Reject. After approval, the
executor runs and the result comes back, but there is **no result
visualization** beyond the text `answer` string. `renderToolOverlay` can
draw marker/radius overlays, but the template-specific shapes (bearing
wedge, fan-in bars, density) are not rendered.

### Gap

The executor returns structured, geospatially-grounded results (entities
with `geom`, `distance`, `tags`), but the frontend only shows the
synthesized text answer. The result entities are not plotted on
`WorldKGMap.vue`, the trace is not shown as a DAG walkthrough, and there is
no way for an external agent to control how results are visualized. This is
what V1 and V2 address.

---

## 3. Core Concepts From Post #1: OSM Renderer / Web Mercator

The post builds a minimal OSM tile renderer in pure JS/Canvas and exposes
the math that Leaflet hides. The transferable concepts:

### 3.1 `latLonToWorld` / `worldToLatLon`: Projection as a First-Class Operation

```js
// Web Mercator (EPSG:3857), normalized to [0,1] then scaled by 2^zoom
const x = (lon + 180) / 360;
const y = 0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI);
const scale = Math.pow(2, zoom);
return { x: x * TILE_SIZE * scale, y: y * TILE_SIZE * scale };
```

**Relevance to result viz:** every result entity has a `geom` (PostGIS
geometry). To plot results on the map at the correct pixel position, the
frontend needs this projection. `WorldKGMap.vue` uses `vue-leaflet`, which
hides it behind `latLngToContainerPoint`. A result overlay either uses that
API or the explicit math from the post for a custom Canvas/SVG layer.

### 3.2 Tile Pyramid + Zoom Scaling: `scale = 2^zoom`

- `TILE_SIZE = 256`, `MAX_LATITUDE = 85.0511287798` (Web Mercator pole limit)
- Visible-tile computation: canvas corners → world coords → tile coords →
  fetch → draw. Tile cache keyed by `z/x/y`.

**Relevance to result viz:** a result overlay must respect zoom level. At
low zoom, N result entities are a cluster of markers; at high zoom, they
spread out and individual geometry becomes visible. The tile-pyramid mental
model is what makes the overlay zoom-consistent with the base OSM raster
layer that `WorldKGMap.vue` already renders.

### 3.3 Visible-Tile Computation + Caching: Viewport-Driven Culling

The post's `getVisibleTiles()` is a tight loop: canvas corners → world
coords → tile coords → fetch → draw. The result overlay needs the analogous
loop: canvas viewport → world bbox → which result entities fall inside →
render those, skip the rest. Same **viewport-driven culling** pattern,
applied to result entities instead of tiles.

### 3.4 Why the Poles "Explode": `MAX_LATITUDE = 85.0511`

The Mercator `y` formula diverges as `φ → ±90°`. The post clamps latitude
to `±85.0511°`.

**Relevance:** the pipeline processes high-latitude countries (Iceland `IS`,
Norway `NO`, Finland `FI`). A result overlay that naively plots `(lat, lon)`
without the clamp will render garbage for subgraph profiles near the poles.
One-line invariant the overlay must inherit.

---

## 4. Core Concepts From Post #2: Spatial Interpolation

The post covers IDW, kernel/RBF interpolation, and reconstructing a
**continuous surface** from **scattered samples**. The transferable
concepts:

### 4.1 IDW (Inverse-Distance Weighting): Already in the Pipeline

The pipeline already uses IDW to reconstruct embeddings from k-NN neighbors
and detect drift (see
`docs/Schematics/05_Learned_Layer/03_Inductive_BallTree_IDW.md`). The post
formalizes the same equation:

```
ŝ(x) = Σ_i w_i(x) · z_i ,   w_i(x) = 1 / d(x, x_i)^p
```

**Relevance to result viz (confidence surface):** each parsed concept has a
`confidence` ∈ [0,1], and the overall query has a confidence. Instead of
showing a single number, a result panel can render a **per-slot confidence
surface** over the role-precedence axis (SUB_COND → COND → SUPPORT →
MEASURE) using IDW across the parsed concepts' individual confidences.
Low-confidence slots become "valleys", visually flagging where the parser
was uncertain, which contextualizes the result quality for the user.

### 4.2 Kernel/RBF Interpolation: Smooth Result Density

The Gaussian RBF kernel `K(x, x') = exp(-||x - x'||² / 2σ²)` produces
smooth interpolation.

**Relevance to result viz (density heatmap):** for templates that return
many entities (template #1 with a dense area, template #5 with a broad
bearing sector), a raw marker scatter can be unreadable. An RBF density
surface over result entity positions, `ρ(x) = Σ_i K(x, x_i)`, produces a
smooth heatmap that communicates clustering structure without per-marker
clutter. This is the post's interpolation theory applied to the result set,
not the embedding space.

### 4.3 Continuous Surface From Scattered Samples: The Mental Model

The deepest transferable idea: **result entities are a sparse sample of the
underlying geographic distribution**. Visualizing them as discrete markers
is honest but limited. Visualizing them as a sampled surface over the query
region communicates both *where* results are *and* *where they aren't* (the
gaps are informative: they may mean "no amenities there" or "the parser
misclassified the area").

---

## 5. V1: Deterministic Result Visualization

**Principle:** each `(template, result_shape)` pair maps to a fixed
visualization component. No agent reasoning, no round-trip, no override.
Predictable, testable, always works.

### 5.1 The Mapping Table

| Template | Result shape | V1 Visualization | Post #1 concept | Post #2 concept |
|---|---|---|---|---|
| #1 FILTER-AGGREGATE-MEASURE | N entities within radius R | Radius circle (R) + ranked markers inside, color-coded by distance | `latLonToWorld` for circle projection | IDW confidence surface on the concept slots |
| #2 OBJECT-FIELD-MEASURE | 1 entity + field value | Single marker + attribute card popup | `latLonToWorld` for marker | — |
| #4 GEOCODE-BATCH-COMPARE | N entities ranked by distance to anchor | Fan-in diagram: anchor marker + N result markers + distance bars | `latLonToWorld` for all markers | RBF density if N is large |
| #5 LOCATION-BEARING-CLASSIFY | N entities in a bearing sector | Bearing wedge from anchor + markers inside the wedge | `latLonToWorld` + bearing calc | RBF density within the wedge |
| #8 PLACE-ATTRIBUTE-QUERY | 1 entity + attribute list | Single marker + attribute list panel | `latLonToWorld` for marker | — |

### 5.2 New Vue Component: `ResultOverlay.vue`

A Leaflet overlay (SVG or Canvas layer) that renders V1 visualizations on
top of `WorldKGMap.vue`. Responsibilities:

1. **Project** each result entity's `geom` with `latLngToContainerPoint`
   (Leaflet API equivalent of post #1's `latLonToWorld`).
2. **Cull** entities outside the current viewport (post #1 §3.3 pattern).
3. **Render** the template-specific viz from the mapping table above:
   - Template #1: `L.circle(anchor, R)` + markers, color scale by distance
   - Template #4: anchor marker + result markers + SVG distance bars
     between each result and the anchor
   - Template #5: SVG bearing wedge (polygon from anchor at angle ±δ) +
     markers inside
4. **Encode confidence** as a small IDW sparkline (post #2 §4.1) below the
   result panel. X-axis = role order, y-axis = interpolated confidence.
5. **Clamp latitude** to `±85.0511°` (post #1 §3.4) for high-latitude
   countries.

### 5.3 Enhanced `QueryConfirmationModal.vue` → Result Panel

After the executor returns, the modal transitions from "confirm query" to
"show result". The result panel has two parts:

1. **Text answer** (existing `answer` string), unchanged.
2. **Map result overlay**, `ResultOverlay.vue` mounted in a mini-map
   (300px tall) showing the V1 visualization. Clicking a result marker in
   the mini-map highlights the corresponding row in a result list below.

This keeps the current HITL flow intact: confirm → execute → visualize, all
in one modal lifecycle.

### 5.4 V1 Properties

- **No MCP round-trip**, visualization is synchronous, determined by the
  template + result shape lookup table.
- **No agent involvement**, the frontend renders the fixed viz as soon as
  the executor response arrives.
- **Testable**, each `(template, result_shape) → viz` mapping is a pure
  function; unit tests assert the correct component renders for each input.
- **Bounded latency**, rendering is sub-frame, no network dependency.
- **Rigid**, if the result is empty, huge, or spatially weird, the fixed
  viz may not communicate it well. That is what V2 solves.

---

## 6. V2: Agent-Controlled Visualization

**Principle:** the executor result + trace + DAG are handed to an agent via
MCP. The agent selects which visualization to invoke from a palette of
available viz tools, and can override the V1 deterministic choice when the
result shape warrants it.

### 6.1 New MCP Tool: `renderResultViz`

```ts
// frontend-v3/src/mcp/mcp-server.ts
server.registerTool(
  "renderResultViz",
  {
    description:
      "Render a visualization for the current MapQA executor result. " +
      "The agent selects a viz type from the palette; the frontend " +
      "mounts the corresponding component on WorldKGMap.vue and returns " +
      "a render acknowledgment. Overrides the V1 deterministic default.",
    inputSchema: {
      viz_type: z
        .enum([
          "radius_circle",      // template #1 default
          "fan_in_distance",    // template #4 default
          "bearing_wedge",      // template #5 default
          "single_marker",      // templates #2, #8 default
          "density_heatmap",    // RBF density (post #2 §4.2)
          "convex_hull",        // result spatial extent
          "trace_dag_walk",     // trace steps as DAG nodes on map
          "empty_state",        // no results — show query region only
          "confidence_surface", // IDW per-slot confidence (post #2 §4.1)
        ])
        .describe("Which visualization to render"),
      options: z
        .record(z.string(), z.any())
        .optional()
        .describe("Viz-specific options (radius, bearing angle, heatmap bandwidth, etc.)"),
    },
  },
  async (params) => {
    const result = await mcpHandlers.renderResultViz(params);
    return { content: [{ type: "text" as const, text: JSON.stringify(result) }] };
  },
);
```

### 6.2 New MCP Tool: `getExecutorResult`

The agent needs to see the result to decide which viz to render. This tool
exposes the executor output that is already in the proposal store:

```ts
server.registerTool(
  "getExecutorResult",
  {
    description:
      "Get the current MapQA executor result: template, results (with " +
      "geometry), answer, trace, and latency. The agent uses this to " +
      "decide which visualization to render via renderResultViz.",
  },
  async () => {
    const result = await mcpHandlers.getExecutorResult();
    return { content: [{ type: "text" as const, text: JSON.stringify(result) }] };
  },
);
```

### 6.3 The V2 Loop

1. Executor returns `{template, results, answer, trace, latency_ms}`.
2. Frontend renders the V1 deterministic viz immediately (no wait).
3. Concurrently, the agent calls `getExecutorResult` via MCP and inspects
   the result shape:
   - `result_count == 0` → "empty_state"
   - `result_count > 200` → "density_heatmap"
   - spatially scattered → "convex_hull"
   - user asked for trace → "trace_dag_walk"
   - low confidence → "confidence_surface"
   - otherwise → keep V1 default
4. The agent calls `renderResultViz(viz_type, options)`.
5. The frontend swaps the V1 viz for the agent-selected viz.

The key design choice: **V1 renders first, V2 overrides if needed.** The
user is never staring at a blank screen waiting for the agent. The agent's
role is *refinement*, not *gatekeeping*.

### 6.4 Viz Palette (Agent-Selectable)

| `viz_type` | When the agent would choose it | Post concept |
|---|---|---|
| `radius_circle` | Template #1, small N (default V1) | Post #1 §3.1 |
| `fan_in_distance` | Template #4, small N (default V1) | Post #1 §3.1 |
| `bearing_wedge` | Template #5 (default V1) | Post #1 §3.1 + bearing |
| `single_marker` | Templates #2, #8 (default V1) | Post #1 §3.1 |
| `density_heatmap` | Large N (>200 entities), marker clutter | Post #2 §4.2 (RBF) |
| `convex_hull` | Spatially scattered results, show extent | Post #1 §3.1 (project hull vertices) |
| `trace_dag_walk` | User/debugger wants to see execution steps | Post #1 §3.1 (project trace step anchors) |
| `empty_state` | `result_count == 0`, show query region | Post #1 §3.1 (project the anchor) |
| `confidence_surface` | Low parser confidence, show uncertainty | Post #2 §4.1 (IDW) |

### 6.5 V2 Properties

- **One MCP round-trip**: `getExecutorResult` + `renderResultViz`. Latency
  depends on agent inference time, not on rendering.
- **Agent judgment**: the agent looks at the actual result shape and picks a
  better viz than the fixed default. This is where the flexibility pays off.
- **V1 fallback**: if the agent is unavailable, slow, or errors, the V1
  deterministic viz stays on screen. V2 is an enhancement layer, not a
  dependency.
- **Testable via simulation**: feed the agent candidate result shapes and
  assert it picks the expected `viz_type`. Harder to test than V1 but
  tractable with a fixed eval set.

---

## 7. Why These Concepts Fit This Project

| Post concept | V1 integration point | V2 integration point |
|---|---|---|
| Web Mercator `latLonToWorld` | `ResultOverlay.vue` projects result geoms | All agent-selected vizs that place entities on the map |
| Tile pyramid + zoom scaling | Zoom-consistent result overlay | Same, all map-based vizs |
| Visible-tile culling loop | Viewport-driven result entity culling | Same, all map-based vizs |
| `MAX_LATITUDE` clamp | High-latitude country results (IS, NO, FI) | Same, all map-based vizs |
| IDW | Per-slot confidence sparkline in result panel | `confidence_surface` viz type (agent-selectable) |
| RBF kernel | — (V1 doesn't use density) | `density_heatmap` viz type (agent-selectable for large N) |
| Continuous surface from samples | — | Mental model: result entities as sparse sample of the geographic distribution; gaps are informative |

Post #1 concepts are **mechanical**, the same math Leaflet uses, exposed
explicitly. They apply equally to V1 and V2; every map-based viz needs them.
Post #2 concepts are **conceptual**; they add viz *types* that V1 doesn't
need but V2 can invoke when the result shape warrants it. IDW confidence
surfaces and RBF density heatmaps are the two post #2 ideas that become
agent-selectable tools in the V2 palette.

---

## 8. Non-Goals / Out of Scope

- **Replacing Leaflet** with a custom Canvas renderer. The post builds a
  from-scratch renderer for *pedagogy*; this project keeps Leaflet as the
  base layer and adds result overlays. The post's value is the *mental
  model*, not the code.
- **Re-training the parser or executor.** Visualization is a pure frontend
  + MCP concern; no model changes.
- **Production MCP exposure.** The MCP bridge is dev-only (`/__mcp`, not
  exposed in production, see `01_MapQA_Parser_Executor.md` §8). V2's
  `renderResultViz` and `getExecutorResult` tools inherit the same
  constraint.
- **Alert-driven agent invocation.** Out of scope for this document.
  Alerting on high latency, low confidence, or stalled steps is a separate
  concern; this doc focuses on what happens *after* a result is in hand.
- **Viz component API style (Options vs Composition).** Left as an
  implementation decision. The existing `frontend-v3/` codebase uses
  Composition API with `<script setup>`; a DevTool component may
  deliberately use Options API for explicit lifecycle visibility, but that
  choice doesn't affect the V1/V2 architecture described here.

---

## 9. References

- Post #1: <https://spatial-dev.guru/2026/01/11/building-an-openstreetmap-renderer-from-scratch/>
- Post #2: <https://spatial-dev.guru/posts/page/2/> (Spatial Interpolation in Python)
- Parser source: <ref_file file="backend/semantic_search/services/query_parser_service.py" />
