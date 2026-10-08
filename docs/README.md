# Documentation Overview

The WorldKG pipeline transforms heterogeneous, noisy, unstructured
OpenStreetMap data into homogeneous, clean, structured knowledge. It
ingests OSM planet data, builds vector embeddings, aligns entities with
Wikidata, predicts spatial links, and learns the spectral structure of
the resulting graph. The artifacts are consumed at runtime by a Vue 3 +
Leaflet application and an MCP-backed agent tool surface, where a
natural-language question becomes a structured query, an executed
result, and a grounded answer.

The foundational papers divide into three jobs, and the docs follow the
same division. `docs/papers/WorldKG/` cleans the unstructured OSM data.
`docs/papers/WernerKuhn/` interprets the data. `docs/papers/Maps/`
supplies the domain expertise. This overview tells that story end to
end: which paper contributes which mathematical idea, where the
pipeline embeds it, and how the frontend turns the batch artifacts into
a queryable application. `docs/SETUP.md` covers environment setup,
Docker, migrations, and operational commands. `docs/Schematics/` is the
architectural truth with file:line references to production source.
`docs/preserving_the_process/` is the per-paper preservation record.

---

## The foundational papers: three tiers

Eleven papers across three subfolders of `docs/papers/` ground the
system. Each contributes a mathematical idea the pipeline preserves
natively in Django + Celery rather than as isolated scripts. The tiers
are not three sequential pipeline stages. They are three jobs: build
the clean data, define how it gets interpreted, and supply the math
that measures the resulting structure.

### Tier 1: clean the unstructured OSM data (`docs/papers/WorldKG/`)

Six papers define the batch pipeline. Their job is to turn raw OSM into
a queryable knowledge graph: normalize free-text tags into a schema,
align entities to Wikidata identity, embed semantics, and predict
relations the raw data never states.

**WorldKG: A World-Scale Geographic Knowledge Graph** scales OSM to a
global KG. The pipeline embeds this as the database-backed
`OSMWikiDataHierarchy` (Planet → Continent → Country → Subgraph), making
world-scale extraction a routine Celery canvas instead of a multi-day
ordeal. Step 1b populates `wkg_class` on every entity via a SQL-side
ontology join against the WorldKG TTL ontology.

**Iterative Geographic Entity Alignment with Cross-Attention (IGEA)**
aligns OSM entities to Wikidata entries. The paper replaces generic
cosine similarity with a bidirectional LSTM cross-attention layer: a
shared BiLSTM encodes both the OSM label and the Wikidata candidate
label, then a cross-attention matrix `softmax(osm_proj · wd_contextᵀ)`
learns cross-cultural semantic variations. Step 3 runs this as an
iterative bootstrapping loop (seed → train → score → accept → expand,
`MAX_ITERATIONS = 3`, threshold 0.6). The pipeline feeds it clipped,
versioned regional PBFs so the transformer converges without noise.

**GeoVectors: A Linked Open Corpus of OpenStreetMap** bridges sparse
OSM tags to a continuous semantic space. The paper defines two
embedding axes: GV-Tags (300D semantic, FastText token-mean
aggregation) and GV-NLE (100D spatial, DeepWalk on a k-NN graph). The
pipeline computes both. GV-Tags uses weighted FastText aggregation,
`synthesized_vector += (weight * fasttext_vec)` where
`weight = count / total_weight`, then L2-normalizes. GV-NLE builds a
k=50 haversine k-NN graph, applies log-inverse-distance-weighted edge
weights `w = max(1/ln(max(d_km, 1.1)), e)`, and runs weighted DeepWalk.
For entities outside the training graph, an inductive BallTree bridge
pulls the 50 nearest GV-NLE vectors and applies the same damped weight.

**Spatial Link Prediction with Spatial and Semantic (USLP)** predicts
spatial relationships between entities. The paper decomposes the score
across three spaces: geographic (geohash distance), name (FastText
cosine), and class (FastText cosine). The pipeline computes
`final_score = geo_score + name_score + class_score` on the fly over
PostGIS, with `geo_score = 1 - d/d_max` (per-tail-cluster `d_max`,
clamped to [0,1]), relation-specific geohash precision (1 ≈ 5000 km,
3 ≈ 156 km, 4 ≈ 39 km), and a `TorchUSLP` GPU path for batched scoring.

**Linking OpenStreetMap with Knowledge Graphs** defines the link
discovery methodology between OSM tags and Wikidata properties,
materialized as the Step 1b SQL ontology join.

**Attention-Based Vandalism Detection in OpenStreetMap** is referenced
for its cross-attention architecture. It is not a live pipeline stage.

### Tier 2: interpret the data (`docs/papers/WernerKuhn/`)

Three papers ground the interpretation layer: how the cleaned data
becomes answerable at request time. They supply a vocabulary for what a
question can ask about the data (Kuhn), a parser that maps natural
language onto that vocabulary (MapQA), and an agent loop that composes
plans and calls tools (Spatial-Agent).

**Core Concepts of Spatial Information for Transdisciplinary Research**
defines five language-independent concepts: Location, Neighborhood,
Field, Object, Network. The MapQA parser maps a natural-language
question into these concepts, and the 9 execution templates map
directly to them.

**MapQA: Open-domain Geospatial Question Answering** frames geospatial
QA as template classification. The pipeline preserves the paper's
approach: a TF-IDF + MultinomialNB classifier assigns the question to
one of 5 trained templates, a one-vs-rest Logistic Regression extracts
concepts, and a Logistic Regression role assigner maps concepts to DAG
roles (SUB_COND → COND → SUPPORT → MEASURE) following the G2 precedence
constraint. The parser is trained on the public MapQA dataset plus
self-supervised Q&A pairs generated from real OSM data.

**Spatial-Agent: Agentic Geo-spatial Reasoning with Scientific Core
Concepts** defines the agentic loop and the GeoFlow DAG. The pipeline
externalizes the agent's tool palette as an MCP server (9 tools, Goose
+ qwen3:8b on Ollama). The GeoFlow DAG, a directed acyclic graph of
spatial concept transformations, is what the parser produces and the
executor walks in topological order.

### Tier 3: domain expertise (`docs/papers/Maps/`)

Two papers supply the math substrate for the learned layer (Steps 5c
and 5d) and the cross-subgraph transport that lets large countries run
on a single GPU. They are not ETL stages; they are the machinery the
interpretation layer's Network concept runs on.

**Spectral Maps for Learning on Subgraphs** defines the spectral
eigenbasis, community structure, and heat kernel diffusion that become
factor-node tables. Step 5c computes the graph Laplacian `L = D - W`,
its top-k eigenvalues, the Fiedler vector (algebraic connectivity λ₂),
and Louvain communities, with WorldKG classes encoded as graph signals
measured by Dirichlet energy `sᵀLs`.

**Latent Functional Maps: A Spectral Framework for Representation
Alignment** defines the functional-map transport matrices that bridge
adjacent subgraph eigenbases. For countries ≥ 5M nodes, Step 5c splits
the spectral solve across subdivisions and stores transport matrices in
`factor_subgraph_transport`, preserving cross-subgraph diffusion at
runtime without a global solve.

---

## The pipeline canvas

The pipeline is a Celery canvas (chain + chords) of country-level
steps. The frontend triggers a country run; Celery orchestrates the
stages.

| Step | Description | Paper | Tier |
|---|---|---|---|
| 0. Planet Init | Extract continents, pre-build hierarchy, country paths, subgraphs, Wikidata IDs | WorldKG | 1 |
| 1. Embed OSM Entities | Stream PBF, upsert `OsmEntity`, compute GV-Tags 300D | GeoVectors | 1 |
| 1b. WorldKG Enrichment | Populate `wkg_class` via SQL ontology join | WorldKG | 1 |
| 2. Harvest Wikidata | SPARQL bbox harvest + P31/P279* class enrichment | IGEA upstream | 1 |
| 3. Run IGEA | Iterative BiLSTM cross-attention entity alignment | IGEA | 1 |
| 4. Predict Spatial Links | USLP tri-space scoring (CPU or `TorchUSLP` GPU) | USLP | 1 |
| 5. Train GV-NLE | k-NN graph + weighted DeepWalk → `gv_nle_embedding` 100D | GeoVectors | 1 |
| 5c. Graph Spectral | Laplacian eigenvalues, Fiedler vector, communities, factor tables | Spectral Maps | 3 |
| 5d. Temporal Drift | Spectral drift + embedding drift across snapshots | Spectral Maps | 3 |
| 6. Ready | Mark country ready for query | (none) | - |

Tier 1 is the batch pipeline (Steps 0-5). Tier 3 writes the factor
tables (Steps 5c, 5d). Tier 2 runs at request time: it turns the
cleaned data and factor tables into answers, never as a batch step.

Two design principles hold the canvas together. First, the
administrative region (`admin_level=2` and `admin_level=4`) is the data
primitive: the IGEA transformer and the GeoVectors embedding layer are
aligned on the same physical + cognitive ground truth. Second, a
temporal geofence (`osmium time-filter`) ensures boundaries and
entities match the historical state of the world relative to the
training data, avoiding the temporal drift failure where 2024 boundary
data validates 2018 entity extractions.

See `preserving_the_process/PIPELINE_INTENT_OVERVIEW.md` for the full
step table and `Schematics/02_Celery/01_Celery_Control_Plane.md` for the
canvas structure.

---

## From batch artifacts to runtime queries

The key design decision is that all expensive computation happens
during the pipeline run, not at request time. The spectral
eigendecomposition, community detection, drift computation, and amenity
embeddings are batch-written to factor-node tables during Steps 5c, 5d,
and `init_planet`. At request time the application queries these tables
via SQL + pgvector. A heat kernel diffusion query becomes a single
pgvector `<#>` inner-product lookup. A community summary becomes a
filtered aggregate. A temporal drift query becomes a join between
snapshot pairs. No graph loading, no eigendecomposition, no SciPy at
request time.

The factor-table path is authoritative. Four tables hold the learned
layer: `factor_spectral_node_metric` (eigenvalues, λ₂, gap per entity),
`factor_drift_node_metric` (drift metrics per entity per snapshot pair),
`factor_amenity_embedding` (amenity semantic vectors), and
`factor_subgraph_transport` (cross-subgraph transport matrices). The
runtime `FactorResolutionService` resolves spectral, diffusion,
community, and amenity queries against these tables. The legacy
runtime graph path (GraphML → NetworkX → SciPy) is removed.

Semantic search is exact (the `+ 0` form blocks the HNSW index); the
executor re-ranks by `combined_score = diffusion_score + name_score +
geo_score + uslp_boost`. Proper-name queries ("Island Grill within 150km
of X") get a name-first pool tier (trigram on `name_romanized`) merged
into the FastText pool, and the parser recovers a missing OBJECT for
brand names by geocoding the span and checking its WorldKG class — no
retraining. See
`preserving_the_process/DIFFUSION_SCORE_AND_QUERY_FUSION.md` for the
fusion provenance and `docs/issues/HNSW_RELEVANCE_AND_USLP_RERANK_DESIGN.md`
for the exact-search decision.

See `Schematics/05_Learned_Layer/05_Factor_Node_Runtime_Joins.md` for
the table schemas and `Schematics/05_Learned_Layer/09_Factor_Table_Math_Reference.md`
for the complete math, including a worked heat-kernel example as one
pgvector query.

---

## Templates as tools

The interpretation layer's output is a template: a structured query
skeleton with concept slots and roles. Each template is a tool. It
declares what kind of question it answers, takes the extracted concepts
as arguments, and executes against the factor-table + PostGIS stack.
The tool surface is implemented as an MCP server
(`frontend-v3/src/mcp/mcp-server.ts`):

| Tool | Job |
|---|---|
| `templateQuery` | Route a natural-language question through the parser into the executor, over the ~13-template library. This is the template tool. |
| `structuredSearch` | Structured OSM tag search with tri-space scoring (name + geo + class). A `name` tag routes to the romanizer-backed fuzzy name path. |
| `getFactorAvailability` | Gate factor-table coverage (G4) before a query runs: which latent spaces exist for a country and snapshot. |
| `proposeQuery`, `getApprovalState`, `getQueryProposal` | Human-in-the-loop loop: push a parsed query to the frontend, read approval state, fetch the proposal. |
| `renderToolOverlay` | Draw a tool's output on the map as a dedicated overlay. |

### Template status

Nine numbered templates are implemented. Five are trained and emitted
by the parser. Four spectral templates are wired in the executor and
resolve via factor tables, but the parser does not emit them.

**Trained and parser-emitted:**

| # | Template | Kuhn concepts | Execution path |
|---|---|---|---|
| #1 | FILTER-AGGREGATE-MEASURE | Object, Location | PostGIS `ST_DWithin` + count |
| #2 | OBJECT-FIELD-MEASURE | Object, Field | Haversine distance |
| #4 | GEOCODE-BATCH-COMPARE | Object, Neighborhood | PostGIS distance ordering |
| #5 | LOCATION-BEARING-CLASSIFY | Location, Neighborhood | PostGIS bearing calc |
| #8 | PLACE-ATTRIBUTE-QUERY | Object, Field | Heat kernel via `factor_spectral_node_metric`, PostGIS fallback |

**Sample questions (trained templates):**

| # | Template | Description | Sample question |
|---|---|---|---|
| #1 | FILTER-AGGREGATE-MEASURE | Count of amenity entities within a radius | "Which bars are within 50m of Hollywood Blvd?" |
| #2 | OBJECT-FIELD-MEASURE | Distance in kilometers between two entities | "How far is the cafe from the museum?" |
| #4 | GEOCODE-BATCH-COMPARE | Closer candidate / nearest entity of a given type | "Which is closer to the park, the cafe or the museum?" |
| #5 | LOCATION-BEARING-CLASSIFY | Cardinal direction / nearest entity in a direction cone | "Which direction is the airport from the city centre?" |
| #8 | PLACE-ATTRIBUTE-QUERY | Amenity attribute / adjacent entity of a given type | "What amenity is available at the museum?" |

The descriptions are the `TEMPLATE_META` strings; the sample questions
follow the training-data frames in
`backend/semantic_search/services/mapqa_samplers/constants.py`
(`FRAMES`), slots filled with concrete values. The #1 example is the
`templateQuery` tool description verbatim.

**Wired in the executor, not parser-emitted:**

| # | Template | Kuhn concepts | Execution path |
|---|---|---|---|
| #11 | SPECTRAL-ANALYSIS | Network | `factor_spectral_node_metric` |
| #12 | TEMPORAL-DRIFT | Network | `factor_drift_node_metric` |
| #13 | COMMUNITY-DETECT | Network | communities from `factor_spectral_node_metric` |
| #14 | EVENT-DIFFUSION | Network | heat kernel diffusion from `factor_spectral_node_metric` |

### Coverage across Kuhn's concepts

| Kuhn concept | Tool status | Where |
|---|---|---|
| Location | covered | `LOCATION` concept; FILTER-AGGREGATE-MEASURE, GEOCODE-BATCH-COMPARE, LOCATION-BEARING-CLASSIFY |
| Object | covered | `OBJECT` concept; all 5 trained templates |
| Field | partial | PLACE-ATTRIBUTE-QUERY, OBJECT-FIELD-MEASURE; used for amenity attributes, not continuous phenomena |
| Neighborhood | implicit | no dedicated concept slot; radius `AMOUNT` + `ST_DWithin` |
| Network | dormant | #11-14 wired in the executor, not parser-emitted |

### The gap

Two gaps remain in category coverage. The MapQA paper describes a
10-template library; 5 are trained, the remaining 5 have 0% coverage
and are not trained. And the 4 spectral templates, which realize Kuhn's
Network concept, are unreachable from natural language: the parser
never emits them, even though the factor tables they read now exist.
Closing the gap means training the remaining paper templates and wiring
the parser to emit the spectral templates. The tool surface already
handles either case: `templateQuery` routes any emitted template
through parser and executor unchanged.

### Execution: parser to answer

**Parser.** A TF-IDF + MultinomialNB classifier assigns the question to
one of 5 trained templates. A one-vs-rest Logistic Regression concept
extractor pulls out amenity, location, radius, and open-vocabulary
OBJECT concepts. A Logistic Regression role assigner maps concepts to
DAG roles following the G2 precedence constraint (SUB_COND → COND →
SUPPORT → MEASURE). The output is a GeoFlow DAG, directly mirroring
the Spatial-Agent paper's concept transformation formalism. Below
`MAPQA_LLM_FALLBACK_CONFIDENCE` (default 0.5), the parser falls back to
an LLM for template assignment.

**Executor.** The executor walks the DAG in topological order and
dispatches to template-specific handlers. Each handler uses the
factor-table path as authoritative, with PostGIS as the spatial
fallback. Amenity resolution uses a 3-tier fallback: exact OSM tag
(GIN-indexed JSONB containment) → WorldKG ontology class → FastText
semantic (`gv_tags_embedding <=> query_vec`, exact cosine via `+ 0`).
The 3-tier trace is shown to the user so every resolution step is
visible.

**HITL.** The parsed query is presented as an editable confirmation
modal via the MCP bridge. The user approves, edits concept slots, or
rejects. Only approved queries proceed to execution. This is the
transparency layer: the user sees what the parser understood before any
computation runs.

**Enrichment and streaming.** After execution, the direct path fetches
deterministic context (USLP spatial links, factor-table communities,
class distribution) via three indexed SQL queries, then makes one LLM
call grounded in the context digest. The agent path retains the
Spatial-Agent research loop for MCP agents. Results stream to the
frontend over SSE: `parsed` → `executed` → `context` → `answer_delta` →
`done`.

See `preserving_the_process/MAPQA.md` for the parser, executor, and
enrichment details, `preserving_the_process/SPATIAL_AGENT.md` for the
MCP tool surface, `preserving_the_process/KUHN_CORE_CONCEPTS.md` for
the category-coverage analysis, and
`Schematics/05_Learned_Layer/01_MapQA_Parser_Executor.md` for the
implementation.

---

## How the documentation is organized

| Path | What lives here |
|---|---|
| `Schematics/` | Architectural truth: 8 concept subdirectories (Encoder, Celery, Postgres, ETL/Django/Vue primitives, Learned Layer, IGEA/USLP, Romanizer, Agent/MCP/LLM) with file:line references to production source. Rewritten in the owner voice per `.devin/rules/10-doc-voice-spec.md`. |
| `preserving_the_process/` | Research-preservation docs: how each academic paper's intent is embedded in the pipeline. `PIPELINE_INTENT_OVERVIEW.md` is the master overview; per-paper docs (`IGEA.md`, `GEOVECTORS.md`, `USLP.md`, `MAPQA.md`, `KUHN_CORE_CONCEPTS.md`, `SPATIAL_AGENT.md`, `OSM2KG.md`, `WORLDKG_ENRICHMENT.md`, `WORLDKG_CDD_ARCHITECTURE.md`, `OSM_TO_BUILT_FORM.md`) cover each. `DIFFUSION_SCORE_AND_QUERY_FUSION.md` records the query-fusion provenance and the diffusion score. |
| `papers/` | The source PDFs, one subfolder per tier: `WorldKG/` (clean), `WernerKuhn/` (interpret), `Maps/` (domain expertise). |
| `SETUP.md` | Environment setup, Docker, migrations, planet init, romanization, fresh-DB reset, overrides. |

### By audience

- **Operators** running the system: start at `SETUP.md` for environment
  and commands, then `Schematics/04_ETL_Django_Vue_Primitives/` for the
  Docker entrypoint and `init_planet` architecture.
- **Engineers** changing the pipeline: start at `Schematics/README.md`
  for the 8-concept map with file:line refs, then
  `preserving_the_process/PIPELINE_INTENT_OVERVIEW.md` for the
  step-by-step paper alignment, then the relevant `Schematics/`
  subdirectory.
- **Researchers** tracing a paper to its implementation: start at the
  paper table above, then the matching `preserving_the_process/` doc,
  then the `Schematics/` subdirectory for the production code
  references.
