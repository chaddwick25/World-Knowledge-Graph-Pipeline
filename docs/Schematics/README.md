# Schematics (Public) — Architectural Truth for the WorldKG Pipeline

> **Purpose:** the canonical public set of schematics for the WorldKG
> pipeline, rewritten in the owner voice per the doc voice spec
> (`.devin/rules/10-doc-voice-spec.md`). Each schematic describes **how the
> code was implemented to achieve the intended function**, with file:line
> references to the production source. Schematics are not decorative. They
> encode relational structures that the code must reflect.
>
> The originals live in `docs/Schematics/`; this tree is the rewritten
> public set. Facts, tables, and formulas are identical; AI-isms and ASCII
> box art are removed.

---

## Core Concepts

The pipeline is organized around eight core concepts. Each concept has a
dedicated subdirectory with one or more schematics.

### 1. Encoder — `01_Encoder/`

How OSM entities become vectors: the two-axis encoding (semantic 300D
GV-Tags + spatial 100D GV-NLE), the streaming pattern that makes
planet-scale PBF processing possible on a bounded-memory worker, and the
BallTree pickle that feeds inductive encoding of unseen entities.

| Doc | Focus |
|---|---|
| [01_Two_Axis_vs_Single_Axis_Encoding.md](01_Encoder/01_Two_Axis_vs_Single_Axis_Encoding.md) | `DBOnlyWriter` vs `DualEncodingWriter` (two-axis Step-1 path, dormant), the `has_pretrained_nle` + `pickle_path` decision branch, the `_PARALLEL_DUAL_ENABLED` phase gate, verified production state |
| [02_Streaming_Batched_Encoding.md](01_Encoder/02_Streaming_Batched_Encoding.md) | SAX-style `pyosmium.SimpleHandler` streaming, chunked DB flush, `BatchCollector` bounded-queue fan-out. Why a 70 GB planet PBF fits on a 4 GB worker |
| [03_BallTree_Pickle_Generation.md](01_Encoder/03_BallTree_Pickle_Generation.md) | PostGIS GiST → entity load → sklearn `NearestNeighbors(haversine, ball_tree)` → IDW damped weights → k-NN graph → DeepWalk → pickle |

### 2. Celery — `02_Celery/`

How Celery acts as the **control plane** for the country pipeline: the
chain + chord canvas, the `PatchedDatabaseBackend` that fixes the
ChordCounter bug, the `@pipeline_step` decorator that separates control
plane (logging, WS push, run tracking) from data plane (services).

| Doc | Focus |
|---|---|
| [01_Celery_Control_Plane.md](02_Celery/01_Celery_Control_Plane.md) | Canvas structure (chain + 3 chords + Steps 5c/5d), chord-in-chain dispatch, `PatchedDatabaseBackend` polling, control-plane vs data-plane separation, `CountryEnvelope`/`PlanetEnvelope` |

### 3. Postgres — `03_Postgres/`

How PostgreSQL serves as the unified storage stack: pgvector for embedding
storage + ANN search (design, not built), PostGIS for spatial indexing, SQL
joins for ontology-driven enrichment, and DB-backed metrics tracking for
the pipeline.

| Doc | Focus |
|---|---|
| [01_Vector_Storage_and_Partitioning.md](03_Postgres/01_Vector_Storage_and_Partitioning.md) | Two-DB topology (default + vectors), `OsmEntity` schema, `PARTITION BY LIST (snapshot_id)` → `LIST (country_code)`, HNSW index lifecycle, per-leaf strategy |
| [02_SQL_Join_Enrichment.md](03_Postgres/02_SQL_Join_Enrichment.md) | `sql_enrich_region()`: ontology → temp table → `jsonb_each_text` → `UPDATE...FROM` join with `ROW_NUMBER()` for the deepest match. 10x faster than the Python loop |
| [03_GIS_Vector_Metrics.md](03_Postgres/03_GIS_Vector_Metrics.md) | PostGIS GiST for bbox/KNN, pgvector 400D ANN (design, not built 2026-09-09), `PipelineRun`/`SnapshotJob`/`PipelineLogEntry` for metrics tracking |

### 4. ETL using Django/Vue as Primitives — `04_ETL_Django_Vue_Primitives/`

How the pipeline uses a traditional SaaS wrapper (Django + Vue 3) around
an ML pipeline: the `init_planet` Docker entrypoint step (batch
preprocessing), OSM-Wikidata primitives instantiated in DB then hydrated
into map configs for visualization, and the Wikidata class loading → SQL
join enrichment cross-step flow.

| Doc | Focus |
|---|---|
| [01_Docker_Entrypoint_Init.md](04_ETL_Django_Vue_Primitives/01_Docker_Entrypoint_Init.md) | `init_planet` management command (16 steps), `PlanetSnapshot` soft lock, `RUN_INIT_PLANET` env var, idempotent batch preprocessing |
| [02_OSM_Wiki_Primitives.md](04_ETL_Django_Vue_Primitives/02_OSM_Wiki_Primitives.md) | `country_relations.json` glue layer, DB hydration → `is_geovectors_supported` → Vue map eligibility filtering, slug conventions |
| [03_Wikidata_Enrichment_Flow.md](04_ETL_Django_Vue_Primitives/03_Wikidata_Enrichment_Flow.md) | Cross-step flow: entry script loads TTL → Redis → Step 1 upserts entities → Step 1c SQL join enriches `wkg_class` using the Redis ontology cache |
| [04_Polyfile_Versioning.md](04_ETL_Django_Vue_Primitives/04_Polyfile_Versioning.md) | Country-level polyfiles are static (not date-accurate); subgraph polyfiles are timestamped + date-accurate; `resolve_country_bbox()` gap; Geofabrik approach |
| [05_Slug_Gate_And_RDF_Namespaces.md](04_ETL_Django_Vue_Primitives/05_Slug_Gate_And_RDF_Namespaces.md) | The two "alignment" layers: (1) country slug gate, `canonical_slug` + `overrides.json` vs `normalize_country_slug(canonical_name)` (NL Step-1 crash 2026-08-26, fixes, Geofabrik index bugs VU/MH/grouped/non-sovereign); (2) RDF namespaces, WorldKG 1.0 (`www.worldkg.org/schema|resource`) vs paper's GeoVectors v2 vs bogus `schema.worldkg.org` (0-class bug, fixed). Full sources/URLs in §3 |

### 5. Learned Layer — `05_Learned_Layer/`

How the pipeline learns: the MapQA parser that aligns natural-language
queries to geospatial templates, subdivision-based batch training for
semantic search (Step 5), and the BallTree + log-inverse distance weighting
used to inductively encode unseen entities from PostGIS spatial indexes.

| Doc | Focus |
|---|---|
| [01_MapQA_Parser_Executor.md](05_Learned_Layer/01_MapQA_Parser_Executor.md) | 5 original templates, TF-IDF + MultinomialNB classification, concept/role models, PostGIS executor, 3-tier amenity fallback, HITL, self-supervised training data (`generate_mapqa_training_data`, PostGIS-verifiable Q&A from `OsmEntity` ground truth, train split = california+augmented+self_supervised, Illinois zero-shot headline metric) |
| [08_MapQA_Parser_Operations_Flowchart.md](05_Learned_Layer/08_MapQA_Parser_Operations_Flowchart.md) | End-to-end flowchart of the MapQA Parser feature: generation → training → runtime phases, every generated asset (CSVs, pickles, JSONs, runtime outputs), and the self-supervision loop |
| [02_Subdivision_Batch_Training.md](05_Learned_Layer/02_Subdivision_Batch_Training.md) | Subdivisions as parallel work units (not partitions), k-NN O(N²) memory, admin-boundary split, Step 5 chord fan-out, GPU slot locks |
| [03_Inductive_BallTree_IDW.md](05_Learned_Layer/03_Inductive_BallTree_IDW.md) | BallTree + IDW damped weights for unseen entities, PostGIS GiST → BallTree → proximity-weighted mean of k=50 nearest GV-NLE vectors |
| [04_Graph_Spectral_Drift.md](05_Learned_Layer/04_Graph_Spectral_Drift.md) | Step 5c (Laplacian eigenvalues, Fiedler vector, Dirichlet energy, Louvain communities) + Step 5d (spectral drift, ARIMA forecast, CUSUM) + embedding drift (Sliced Wasserstein Distance) + 4 new MapQA templates. Step 5c routing: < 5M nodes → country-level GPU LOBPCG; ≥ 5M → subdivision-scoped solves + functional-map transport matrices |
| [05_Factor_Node_Runtime_Joins.md](05_Learned_Layer/05_Factor_Node_Runtime_Joins.md) | Batch-written per-entity factor tables (`factor_spectral_node_metric`, `factor_drift_node_metric`, `factor_amenity_embedding`, `factor_subgraph_transport`) on the vectors DB, runtime SQL-only `FactorResolutionService` (pgvector `<#>` diffusion, community summary, amenity lookup, cross-subgraph transport). Legacy runtime graph path removed; factor-table path is authoritative |
| [06_Spectral_Filtering_Chebyshev_vs_Lanczos.md](05_Learned_Layer/06_Spectral_Filtering_Chebyshev_vs_Lanczos.md) | Chebyshev (Defferrard, ChebNet) vs Lanczos (Liao, Lanczos Networks) polynomial filtering on the Laplacian. Chebyshev approach rejected (fails on clustered spectra); subdivision GPU solves adopted instead |
| [07_MapQA_Geographic_Scoring_and_Routing.md](05_Learned_Layer/07_MapQA_Geographic_Scoring_and_Routing.md) | USLP geographic scoring formula (Mann et al. 2023 §3.3, P4 geohash + haversine), template-specific d_max, USLP signal boost (+0.5 for predicted link tails), deterministic radius-query routing override (FILTER-AGGREGATE-MEASURE), PLACE-ATTRIBUTE-QUERY haversine safety guard, tag key-presence filtering + value-match boost |
| [09_Factor_Table_Math_Reference.md](05_Learned_Layer/09_Factor_Table_Math_Reference.md) | The complete math: k-NN graph construction, Laplacians, eigendecomposition, heat kernel as one pgvector `<#>` query (worked example), table-by-table reference, cross-subgraph transport, temporal drift, hardening gaps |

### 6. IGEA + USLP — `06_IGEA_USLP/`

How the pipeline aligns OSM entities with Wikidata (IGEA, Step 3) and
predicts spatial relationships between entities (USLP, Step 4). IGEA uses
iterative bootstrapping with a per-country cross-attention BiLSTM + NCA.
USLP uses tri-space scoring (geo + name + class) with geohash-based
candidate pruning.

| Doc | Focus |
|---|---|
| [01_IGEA_Iterative_Alignment.md](06_IGEA_USLP/01_IGEA_Iterative_Alignment.md) | Iterative bootstrapping loop (seed → train → score → accept → expand), cross-attention BiLSTM, NCA integration, `EntityAlignment` audit model |
| [02_USLP_Spatial_Link_Prediction.md](06_IGEA_USLP/02_USLP_Spatial_Link_Prediction.md) | Tri-space scoring (geo + name + class), geohash precision per relation type, `SpatialTripletScore` model, chunked bulk persistence, SQL aggregate dashboard, "USLP links as a data asset" (head/tail semantics, real coverage, query-time boost vs training-time sampling prior, prior-vs-truth pseudo-labeling boundary) |

### 7. Romanizer — `07_Romanizer/`

How the `romanizing_names` service converts non-Latin and diacritic-bearing
OSM names to a Latin phonetic fingerprint for pg_trgm similarity search,
without any neural model, API call, or network dependency. Deterministic
Unicode decomposition + lookup tables, persisted in `OsmEntity.name_romanized`
and indexed with PostgreSQL's pg_trgm GIN index.

| Doc | Focus |
|---|---|
| [01_Deterministic_Cross_Script_Romanization.md](07_Romanizer/01_Deterministic_Cross_Script_Romanization.md) | Hangul/diacritic/identity romanizers, registry auto-detect, name_romanized column + pg_trgm index, romanize_names command, search integration with the FastText parallel path, scope (phonetic scripts only, Hanzi/Kanji excluded) |

### 8. Agent, MCP & Platform LLM — `08_Agent_MCP_LLM/`

How the three query modes become MCP tools an agent can call, how the local
model is wired into the platform (provider-abstracted, fail-soft), how
execute-query streams over SSE, and how the agent stack runs in Docker with
the GPU dedicated to the application side.

| Doc | Focus |
|---|---|
| [01_MCP_Tool_Surface.md](08_Agent_MCP_LLM/01_MCP_Tool_Surface.md) | 10 MCP tools, two execution homes (Node data tools vs browser app tools), Vite plugin + HMR-WebSocket channel, Streamable HTTP, overlay layer (`overlayStore` → map) |
| [02_Platform_LLM_Enrichment.md](08_Agent_MCP_LLM/02_Platform_LLM_Enrichment.md) | `LLMService` (native Ollama `think: false` vs openai `/v1`), enrichment (direct path: deterministic context + one synthesis call; agent path: research loop), parser refine, LLM call counts |
| [03_SSE_Streaming_Performance.md](08_Agent_MCP_LLM/03_SSE_Streaming_Performance.md) | `execute-query/stream/` SSE protocol (thread + queue), `get_latest_snapshot_id()` cache (executor 2.6s → 61ms), answer cache, `warm_llm`, geography expression index |
| [04_Docker_Agent_Stack.md](08_Agent_MCP_LLM/04_Docker_Agent_Stack.md) | Ollama + Goose services, two-GPU pinning (4070 Ti Super → app, RTX 2070 → pipeline), config-directory mount gotcha, on-demand goose, runbook |
| [05_LLM_Capacity_Envelope.md](08_Agent_MCP_LLM/05_LLM_Capacity_Envelope.md) | qwen3:8b request flow (parser → executor → LLM synthesis) and the VRAM/compute capacity envelope (scheduler-bound, P95 TTFT ~6s at 6 slots, ~1.6 GiB headroom) |
| [06_Research_Orchestrator_Agent_Stream.md](08_Agent_MCP_LLM/06_Research_Orchestrator_Agent_Stream.md) | Research tab agent stream: KE interviewer (4070) → structured brief → decompose/execute/assemble loop (2070), two Ollama daemons on the Docker network, SSE protocol, traceability contract, stream vs batch processing (DMLS Ch 3/7, K80 = batch); the K80 sibling lands here |
| [07_Security_Review_Models.md](08_Agent_MCP_LLM/07_Security_Review_Models.md) | Offensive-security HF models as config auditors: 2-model bake-off (2026-10-05) on a self-hosted deployment's reverse-proxy + middleware configs — a 3B fine-tune found 1 real finding, an 8B red-team fine-tune answered defensively with wrong claims; guidance: brainstorming only, verify everything |

---

## How to Read These Schematics

- Each doc starts with a **Focus** and **Key Idea** block.
- File references use `<ref_file>` / `<ref_snippet>` tags (clickable in the
  IDE) and inline `path:line` citations.
- Diagrams use markdown tables, nested bullet lists, and arrow-only code
  blocks (`→`). ASCII box art is banned (voice spec §10.4).
- When modifying code referenced by a schematic, the schematic is the
  architectural contract. If the code change breaks the schematic's
  invariants, update both.

## Relationship to Other Docs

| Doc | Role |
|---|---|
| `README.md` (root) | Project overview, open-source stack vs. closed-source alternatives, toolset, GUI artifacts |
| `.devin/rules.md` | Development rules, conventions, operational notes, gotchas |
| `AGENTS.md` | Agent-facing architecture notes (post-refactor state) |
| `docs/Schematics/` | Canonical public schematics (owner voice; the `_public` mirror was dropped 2026-09-09) |
| `docs/Schematics/MATH_FORMULA_REFERENCE.md` | The complete formula base (every formula in the schematics, by source doc, byte-identical) plus the group-theoretic reading (group, invariant, equivariance statement per formula family) |
| `docs/Schematics/USEFUL_COMMANDS.md` | Operator command cookbook (compose, Django, osmium, MapQA, factor tables, DB reset) |
| `docs/plans/` | Implementation plans (active) |
| `docs/plans/completed/` | Completed implementation plans |
| `docs/plans/next-stage/` | Coupled plans for the next stage |
| `docs/issues/` | Open issues and incidents |
| `docs/issues/resolved/` | Resolved issues and incidents |
