# Pipeline Intent Overview: Preserving the WorldKG Research

The master overview for the WorldKG extraction pipeline. Explains the high-level strategy for preserving and amplifying the research intent of the original academic papers (WorldKG, IGEA, GeoVectors) within the Django + Celery pipeline.

## 1. The Strategy of Direct Algorithmic Preservation

The implementation philosophy moves away from fragmented "Academic Snippets" (scripts, CSVs, and manual Java runs) toward a **native geospatial engine**. The goal is to ensure that the mathematical intent of the research is embedded in the system's architecture itself.

- **WorldKG Intent**: Scale OSM to a global KG.
    - *Result*: The database-backed hierarchy (`OSMWikiDataHierarchy`) with Planet → Continent → Country → Subgraph makes world-scale extraction a routine process rather than a multi-day ordeal.
- **IGEA Intent**: Iterative alignment of entities using cross-attention.
    - *Result*: By providing clipped and versioned regional PBFs via subgraph generation, the pipeline provides the clean spatial batches required for the IGEA Transformer to converge without noise.
- **GeoVectors Intent**: Bridging sparse tags to a continuous semantic space.
    - *Result*: The extraction workers handle FastText embedding generation on-the-fly during the PBF stream, geofencing the training vectors to their regional topography with optional GPU acceleration.

## 2. The Pipeline Canvas

The pipeline is a Celery canvas (chain + chords) of country-level steps, with planet-initialization pre-build substeps. Frontend triggers the country run; Celery orchestrates the stages:

| Step | Trigger | Description | Paper Alignment |
|------|---------|-------------|----------------|
| **0. Planet Init** | Frontend | Extract continents from planet file, pre-build structure, country paths, subgraphs, Wikidata IDs | Planet-scale extraction |
| **1. Embed OSM Entities** | Frontend → Celery | Stream PBF, upsert `OsmEntity`, compute GV-Tags embeddings | GeoVectors paper implementation |
| **1b. WorldKG Enrichment** | Celery | Populate `wkg_class` on entities via SQL-side ontology join | WorldKG paper implementation |
| **2. Harvest Wikidata** | Celery | SPARQL bbox harvest + P31/P279* class enrichment | IGEA upstream |
| **3. Run IGEA** | Celery | Iterative entity alignment (BiLSTM cross-attention) | IGEA paper implementation |
| **4. Predict Spatial Links** | Celery | USLP tri-space scoring (CPU or GPU via `TorchUSLP`) | USLP paper implementation |
| **5. Train GV-NLE** | Celery | k-NN graph + weighted DeepWalk → `gv_nle_embedding` | GeoVectors paper implementation |
| **5c. Graph Spectral** | Celery | Spectral analysis, community detection, factor tables | Learned layer |
| **5d. Temporal Drift** | Celery | Spectral drift + embedding drift across snapshots | Learned layer |
| **6. Ready** | Celery | Mark country ready for query | — |

**Database-Backed Architecture:**
- **OSMWikiDataHierarchy Model**: Single source of truth for country metadata, eliminating JSON file dependencies
- **HierarchyCacheService**: In-memory hierarchy building from database for fast lookups (`get_or_build_hierarchy()`)
- **SubgraphProfile Model**: Stores subgraph BBOX and filepaths as database ground truth
- **PipelineRun Model**: Stage-level tracking with WebSocket progress updates

## 3. Core Principles of the Pipeline

### A. The Administrative Region as a "Data Primitive"
As the pipeline progresses, `admin_level=2` (Countries) and `admin_level=4` (States) are treated as the fundamental units of data. By linking these spatial boundaries to their **Wikidata/Knowledge Graph** counterparts via the `OSMWikiDataHierarchy` model, the pipeline creates a unified namespace.
*   **Result**: The Transformer (IGEA) and the Embedding Layer (GeoVectors) are aligned on the same "Physical + Cognitive" ground truth.

### B. The Temporal Geofence (Geofabrik Versioning)
Temporal snapshots (`osmium time-filter`) ensure that boundaries and entities match the specific historical state of the world relative to the training data.
*   **Result**: This avoids "Temporal Drift", a common research failure where 2024 boundary data is accidentally used to validate 2018 entity extractions.
*   **Temporal Poly-Fencing**: Each monthly snapshot is paired with a unique, high-resolution `.poly` file and a human-readable `.json` GeoJSON boundary, ensuring zero spatial leakage across time.

### C. GPU-Accelerated Processing
Scalability is not just about speed; it is about **iterative capacity**. By using PyTorch with CUDA support, the pipeline enables GPU-accelerated processing for:
- **GV-NLE Training**: 10-20x speedup for large datasets using PyTorch Geometric (`PyGDeepWalkService`)
- **USLP Link Prediction**: GPU-accelerated tri-space scoring via `TorchUSLP` with batch embedding precomputation
- **TransE Training**: CUDA support for large-scale knowledge graph embedding training

### D. Cartesian-Semantic Synchronization
By using the `osm_id` and associated geometry of the administrative boundary as a master filter for FastText corpus extraction, the pipeline ensures that the "Physical" boundary and the "Semantic" embedding are synchronized. This eliminates regional noise and enforces the project's intent of treating the administrative region as a unified data primitive.

## 4. Navigating the Intent Guides
For detailed breakdowns of specific research implementations, refer to:
- [IGEA Implementation Details](IGEA.md)
- [GeoVectors Semantic Translation](GEOVECTORS.md)
- [USLP Tri-Space Heuristics](USLP.md)
- [OSM2KG Pipeline Standards](OSM2KG.md)

---
*This document is a living record of the system's architecture and will be updated as deeper ML layers are integrated into the WorldKG pipeline.*
