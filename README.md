# WorldKG Pipeline

Productizing open-source models for geospatial reasoning via the [WorldKG Project](https://www.vgiscience.org/projects/worldkg.html).

The pipeline transforms heterogeneous, noisy, unstructured OpenStreetMap data into homogeneous, clean, structured knowledge. It ingests OSM planet data, builds vector embeddings, aligns entities with Wikidata, and predicts spatial links — all orchestrated as a multi-stage ETL pipeline driven by a [Celery](https://docs.celeryq.dev/) canvas.

The artifacts produced will be consumed by agents for geospatial reasoning ([paper](https://arxiv.org/pdf/2601.16965) — not yet implemented).

## Artifacts

| Artifact | Description |
|----------|-------------|
| **GeoVectors Embeddings** | Semantic and spatial embeddings for OSM entities (GV-Tags 300D + GV-NLE 100D) |
| **Wikidata Alignment** | Connects OSM entities to Wikidata entries via the WorldKG ontology and alignment models |
| **Spatial Link Prediction** | Ground-truth style triplets for spatial relationships between entities |
| **WorldKG Enrichment** | Enriches OSM entities with Wikidata metadata and ontology classes |
| **Semantic Search** | Natural-language queries over enriched OSM entities, with optional subdivision filtering by Wikidata QID |
| **Subdivision Search** | Filter search results by administrative subdivision (city/department/state) using Wikidata QIDs resolved via `SubgraphProfile` bbox records |

## Pipeline Types

Both are driven by Celery:

1. **Planet Initialization Pipeline** — Runs once to initialize configs and primitives. See [Planet Initialization Architecture](docs/Schematics/Planet_Initialization_Architecture.md) and [WorldKG Primitives](docs/Schematics/WorkKG_Primities.md).
2. **Country Pipeline** — Produces the artifacts above for a specific country (or synthetic territory).

## Country Pipeline Stages

### 0. Pre-Flight Checks (OVID + Baseline)

**Vandalism Detection with OVID**
- OVID is an attention-based model for OpenStreetMap vandalism detection.
- Scores edits using changeset, user, and context information.
- Used as a pre-flight check to flag or filter suspicious edits before starting a run.

**Embedding & Graph Baseline Validation**
- BallTree produces a pickle from pre-trained model embeddings, converted into a k-NN graph that records a structural baseline (degree distributions, connected-component sizes, local clustering coefficients).
- IDW (inverse-distance weighting) reconstructs vectors and checks for drift between original and interpolated embeddings via PCA neighborhood plots, t-SNE projections, and 1st/kth nearest-neighbor distance histograms.
- Current snapshots are compared against the baseline using KL divergence on degree histograms and KS tests on degrees, clustering coefficients, and component sizes. A 3σ threshold on degree KL divergence flags anomalous structural drift.
- These geometric and structural baselines catch embedding or graph drift before expensive graph-representation learning stages consume stale or corrupted inputs.

### 1. GeoVectors Embeddings

- GV-Tags (semantic, 300D) + GV-NLE (spatial, 100D) for every OSM entity.
- TSV embedding files from the pre-trained model produce pickle files based on entity IDs and haversine distance.
- OSM entity graph is constructed from the pickle file based on OSM IDs.
- Source: https://geovectors.l3s.uni-hannover.de/data

### 2. WorldKG Enrichment

- Ontology-driven class assignment with hierarchical superclass inference.
- Assigns WorldKG ontology classes to OSM entities from Step 1 — useful for NLP tasks.
- Two modes: local prediction (default, O(1) tag→class lookup via Redis ontology cache) or SPARQL endpoint (online, per-entity `rdf:type` queries).
- Wikidata candidate harvest (Step 2) uses batched SPARQL with retry+backoff on 429/502/503/Timeout via `SPARQLRetryMixin` (`semantic_search.utils.sparql_mixin`). Retries enabled (`SPARQL_MAX_RETRIES = 1`, 5s base backoff). Failed batches are logged and skipped; partial enrichment is preferred over blocking. Batch size 100 QIDs per POST request to avoid URL length limits.
- Two services share the mixin: `semantic_search.services.WikidataCandidateService` (10s timeout) and `worldkg_nca.services.WikidataCandidateService` (30s timeout).

### 3. Wikidata Alignment

- IGEA entity alignment connects OSM entities to Wikidata knowledge graph entries.
- Iterative alignment with cross-attention links OSM entities to Wikidata using semantic + geo-spatial features.
- USLP (Step 4) runs independently — no longer gated on IGEA acceptance count.

### 4. Spatial Link Prediction

- USLP discovers relationships between entities using tri-space scoring (geo + name + class).
- Dashboard queries filter by `country_name`, `snapshot_id`, `predicted=True` — optimized by composite index `igea_triplet_csp_idx` on `SpatialTripletScore` (applied to `vectors` DB via `VectorDBRouter`).
- Aggregation uses Django `aggregate()` + `Case/When` for geo/name/class dominance, histogram buckets, and avg confidence in a single SQL round-trip.
- Augmented data sources include Google Places API and `toronto-data` (Django app in backend), or any appropriate open-source data.

### 5. Learned Layer

- Embeddings saved for each entity are used to train graph representation learning models:
  - **FastText**: 300D semantic embeddings via weighted average of tag embeddings (entity-local, no retraining).
  - **DeepWalk**: 100D spatial embeddings via weighted random walks on k-NN graphs (IDW edge weights).

## Quickstart

```bash
# Build images
docker compose -f docker-compose.yml -f compose.override.yml build

# Start databases and Redis
docker compose -f docker-compose.yml -f compose.override.yml up -d postgres-default postgres-vectors redis

# Start backend API + Celery worker
docker compose -f docker-compose.yml -f compose.override.yml up -d backend worker

# Worker configuration:
#   --pool=prefork --concurrency=4  (4 CPU workers for parallel upserts/IGEA/USLP)
#   GPU tasks (GV-NLE training) serialize via fcntl.flock — no CUDA OOM
#   RUN_MIGRATIONS env var: backend runs migrations, worker waits for them

# Start frontend
cd frontend-v3
npx vite --port 5173 --host 0.0.0.0
```

## ML System Design Principles

The engineering decisions in this pipeline are grounded in the following reference texts.
Citations use the `[KEY:Ch#]` convention (e.g. `[DMLS:Ch3]`) so that specific design
choices can be traced back to the relevant chapter.

### References

| Key | Title | Author | Repo / Link |
|-----|-------|--------|-------------|
| `[COHEN]` | Linear Algebra: Theory, Intuition, Code | Mike X Cohen | https://github.com/mikexcohen/LinAlg4DataScience |
| `[HOML]` | Hands-On Machine Learning with Scikit-Learn and PyTorch | Aurélien Géron | https://github.com/ageron/handson-mlp |
| `[STATS]` | Practical Statistics for Data Scientists | Bruce, Bruce & Gedeck | https://github.com/gedeck/practical-statistics-for-data-scientists |
| `[DMLS]` | Designing Machine Learning Systems | Chip Huyen | — |
| `[GRAPH_REP]` | Graph Representation Learning | William Hamilton | — |
| `[GEO_VEC]` | GeoVectors: A Linked Open Corpus of OpenStreetMap Embeddings | L3S Hannover | https://geovectors.l3s.uni-hannover.de/data |

---

### Designing Machine Learning Systems `[DMLS]`

  Pre‑compute expensive steps `[DMLS:Ch3]`
    -> Planet snapshots, continent extracts, and GeoVectors embeddings are computed once and reused.
    -> This turns most workloads into read‑heavy operations instead of re‑processing the planet for each run.
    -> Idempotency pattern for OSM PBF files produced via Osmium tool

  Batch and parallel processing `[DMLS:Ch3]`
    -> Work is batched per country and per subgraph.
    -> Celery prefork pool (4 workers) parallelizes CPU-bound tasks: subgraph NLE pickle generation, IGEA, USLP.
    -> GPU-bound tasks (GV-NLE training, Step 5) serialize via fcntl.flock to prevent CUDA OOM on single-GPU machines.
    -> Migration race prevention: `RUN_MIGRATIONS` env var in `docker-entrypoint.sh` — backend runs migrations, worker waits.
    -> Per-country leaf partitions with right-sized HNSW indexes keep query latency low.

  Structured logging and observability `[DMLS:Ch8]`
    -> Each Celery task logs inputs, outputs, and timing.
    -> Per‑stage metrics (counts, durations, basic quality checks) make it easier to understand where time and failures occur.

  Config‑driven behavior `[DMLS:Ch6]`
    -> OSM-Wikidata pipeline primitives are processed during the initialization phase based on configurations (e.g., country-specific overrides, embeddings available)
    -> Paths, thresholds (e.g., USLP, entropy), and country‑specific overrides are defined in configuration and JSON files, not hard‑coded in the codebase.
    -> OSM uses idomatic patterns and conventions to make the code more maintainable and easier to understand

  Reproducibility `[DMLS:Ch6]`
    -> A pipeline run is defined by code version + configuration + input paths.
    -> You can repeat a run with the same settings to reproduce results.

  Continual learning from rejected links `[DMLS:Ch9]`
    -> USLP rejected links and IGEA low-confidence matches are retained for data augmentation.
    -> Planned: feed rejected spatial links back into SSLP training as hard negatives.

  MLOps: dev on RTX, deploy on K80 `[DMLS:Ch10]`
    -> The pipeline is developed on RTX 4070 Ti SUPER (16GB) but designed to deploy on K80-class GPUs.
    -> GPU memory-safe patterns (batched training, CUDA cache clearing) ensure portability.
    -> HNSW index sizes are tuned per-country leaf so the system works on 48GB/64GB/128GB machines alike.

  Clear interfaces between stages `[DMLS:Ch5]`
    -> Stages communicate via well‑defined artifacts (snapshots, TSVs, database tables).
    -> You can improve a model inside one stage as long as it respects the same input/output format.

  Unified open‑source storage stack (PostgreSQL + pgvector + Redis)
    ->  Postgres is used as the main database for application state + Enforce OSM-Wikidata Hierarchy constraints
    ->  PostGIS‑enabled database for spatial data used in the stages of the pipeline
    ->  pgvector‑backed database for embeddings and vector similarity search.
    ->  Redis is used as the message broker and cache for Celery and the WebSocket channel layer.
    ->  Redis is used to store the WorldKG Ontology and other metadata.

---

### Linear Algebra in the Pipeline `[COHEN]`

  Vector Applications & pgvector `[COHEN:Ch4]`
    -> pgvector uses L2/cosine distance for ANN search over GV-Tags (300D) and GV-NLE (100D) embeddings.
    -> HNSW indexes on `static_embedding` and `gv_tags_embedding` columns enable sub-10ms similarity queries.
    -> The partitioning scheme (per-country leaf partitions) keeps each HNSW index right-sized for the target machine's RAM.
    -> During bulk upserts, HNSW indexes on leaf partitions are dropped and recreated after loading (14x speedup: 5.5s → 0.4s per 20K batch).

  Covariance Matrix & OSM Data Analysis `[COHEN:Ch7]`
    -> The covariance matrix of embedding dimensions reveals correlations in the noisy, heterogeneous OSM tag space.
    -> Used in pre-flight checks to detect embedding drift between snapshots (see Statistical Analysis below).
    -> Helps understand which tag dimensions carry redundant vs. independent information.

  Eigendecomposition, PCA & SVD `[COHEN:Ch13, Ch15]`
    -> PCA is used in pre-flight validation to project local neighborhoods into 2D/3D for visual drift inspection.
    -> SVD underpins the dimensionality reduction used when comparing baseline vs. current snapshot embeddings.
    -> Planned: agentic tool calls will use eigendecomposition to select the most informative embedding dimensions for query-time reasoning.

  Database Partitioning as a Linear Algebra Problem
    -> Partitioning `semantic_search_osmentity` by `country_code` is equivalent to block-diagonalizing the entity-entity similarity matrix.
    -> Each leaf partition's HNSW index operates on a sub-matrix, reducing both memory footprint and query latency.
    -> The partitioned table (`PARTITION BY LIST (snapshot_id)` → `LIST (country_code)`) is created automatically by the pipeline — no manual cutover needed on fresh DBs.

---

### Statistical Analysis in ETL & Data Validation `[STATS]`

  Pre-flight baseline validation `[STATS:Ch3]`
    -> KL divergence on degree histograms compares the structural distribution of the current k-NN graph against the baseline.
    -> KS tests (Kolmogorov-Smirnov) compare degree, clustering coefficient, and component size distributions.
    -> A 3σ threshold on degree KL divergence flags anomalous structural drift before expensive downstream stages run.

  Embedding drift detection `[STATS:Ch4]`
    -> IDW (inverse-distance weighting) reconstructs embeddings from k-NN neighbors; residual error measures drift.
    -> 1st-neighbor and k-th-neighbor distance distributions quantify sparsity and clustering changes.
    -> t-SNE global projections provide qualitative macroscopic cluster inspection.

  Data validation across pipeline stages `[STATS:Ch2]`
    -> Row count assertions after each bulk upsert (e.g., 20K entities per batch in `vector_storage_service`).
    -> Partition pruning verification — queries with `snapshot_id` + `country_code` hit only the target leaf partition.

---

### Hands-On Machine Learning `[HOML]`

  Validate the construction of the pickle files `[HOML:Ch3]`
    -> BallTree indexing for efficient nearest neighbor queries in embedding space
    -> IDW interpolation to reconstruct embeddings from neighbors (inverse-distance weighting)

  Visualize the drift between the original embeddings from the pre-trained model vs the interpolated embeddings `[HOML:Ch8]`
    -> PCA visualization of local neighborhoods to understand embedding structure
    -> t-SNE global projection to identify macroscopic clusters in the embedding space
    -> Distance distribution analysis (1st neighbor, k-th neighbor) to assess sparsity and clustering

  Graph Structural Drift Analysis `[HOML:Ch8]`
    -> k-NN graph construction from spatial embeddings via BallTree for structural analysis
    -> Degree distribution tracking via histograms and KL divergence
    -> Clustering coefficient monitoring to detect topological changes
    -> Connected component size analysis for graph fragmentation detection
    -> 3-sigma drift thresholds to flag anomalous snapshots automatically
    -> KS tests for distributional comparison between baseline and current snapshots

  Batch processing and pre‑compute `[HOML:Ch4]`
    -> Pre‑compute configs and primitives - WorldKG primitives (see `docs/Schematics/WorkKG_Primities.md`) are generated once and reused.
    -> Multi‑core processing with Osmium - Osmium‑tool is used to parallelize low‑level extraction work.
    -> Batch processing - Vector generation and spatial link prediction are run in batches rather than one entity at a time.
    -> Fan‑out processing for subgraphs(wikidata admin=2) - Large countries are split into subgraphs (administrative subdivisions) so work can be processed in parallel. Step 1 dispatches a chord of per-subgraph upsert tasks; Step 5 trains GV-NLE per subgraph (serialized via GPU lock).

Next Steps:
1. Make the project public
   -> Create a GitHub repository
   -> Complete the pre-release tasks(TODOs)
   -> Publish the project
   -> Fully transition to the Gitlab CI/CD pipeline and use their issues tracker(get rid of local TODOs)
   -> Complete the post_release tasks(TODOs)
   -> Update the Documentation (un-comment the docs after reviewing)
2. Research and then implement https://arxiv.org/pdf/2601.16965
3. Investigate how to add support for https://arxiv.org/pdf/2310.00583
