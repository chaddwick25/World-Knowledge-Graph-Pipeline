# WorldKG Pipeline

This is an attempt to productize open source models for geospatial reasoning via the WorldKG Project(https://www.vgiscience.org/projects/worldkg.html).
The WorldKG pipeline's goal is to transform the hetrogeneous, noisy and unstructured OpenStreetMap into homogenous, clean and structured data. 
It ingests OSM planet data, builds vector embeddings, aligns entities with Wikidata, and predicts spatial links. 
All of this is run as a multi‑stage ETL pipeline controlled by a Celery canvas. 
The artifacts produced by the pipeline will be consumed by agents for geospatial reasoning https://arxiv.org/pdf/2601.16965(not yet implemented).

<!-- TODO: Add screenshots of these metrics in the pipeline -->
Artifacts Produced by the Pipeline:
  -> GeoVectors Embeddings - Semantic and spatial embeddings for OSM entities (GV‑Tags 300D + GV‑NLE 100D).
  -> Wikidata Alignment - Uses the WorldKG ontology and alignment models to connect OSM entities to Wikidata entries.
  -> Spatial Link Prediction - Produces ground‑truth style triplets for spatial relationships between entities.
  -> WorldKG Enrichment - Enriches OSM entities with additional information from Wikidata and the ontology.
  -> Semantic Search - Enables natural‑language queries over enriched OSM entities.

There are two main pipeline types, both driven by Celery:
  1. Planet Initialization Pipeline
    Runs once to initialize the configs needed for the pipeline see docs/Schematics/Planet_Initialization_Pipeline.md and docs/Schematics/WorkKG_Primities.md.
  2. Country Pipeline
    Produces the artifacts listed above for a specific country (or synthetic territory).

## Stages in the country pipeline
  0. Pre‑Flight Checks (OVID + Baseline)
      -> Vandalism Detection with OVID
          ==> OVID is an attention‑based model for OpenStreetMap vandalism detection.  
          ==> It scores edits using changeset, user, and context information.  
          ==> The pipeline can use OVID as a pre‑flight check to flag or filter suspicious edits before starting a run
      -> Embedding & Graph Baseline Validation
          ==> Balltree is used to produce a pickle from the embeddings associated with the pre-trained model and then converted into a k-NN graph, which records a structural baseline
              described by degree distributions, connected-component sizes, and local clustering coefficients.
          ==> It reconstructs vectors via IDW (inverse-distance weighting) and checks for drift between original and interpolated embeddings 
              using PCA neighborhood plots, global t-SNE projections, and 1st/kth nearest-neighbor distance histograms.
          ==> The artifacts from the current snapshots are compared  against the baseline the Pre-trained model's metrics: 
              KL divergence on degree histograms and KS tests on degrees, clustering coefficients, and component sizes; a 3σ threshold on degree KL divergence flags anomalous structural drift
          ==> The pipeline can run these geometric and structural baselines as a pre-flight check so embedding or graph drift 
              is caught before expensive graph-representation learning stages consume stale or corrupted inputs.
  1. GeoVectors Embeddings
        -> GV‑Tags (semantic, 300D) + GV‑NLE (spatial, 100D) for every OSM entity
          ==> TSV embedding files from the pre-trained model makes a pickle files based on entity IDs haversine distance
          ==> OSM Entity Graph is construction from pickle file based OSM ids
          ==> Source: https://geovectors.l3s.uni-hannover.de/data
  2. WorldKG Enrichment
        -> Ontology‑driven class assignment with hierarchical superclass inference
          ==> Uses the ontology to assign classes to OSM entities from the previous step
          ==> WorldKG Enrichment uses the ontology to assign classes to OSM entities(this is extremely useful in NLP tasks)  
  3. Wikidata Alignment
        -> IGEA entity alignment connects OSM entities to Wikidata knowledge graph entries 
        -> Iterative Alignment with Cross-Attention is implemented used to link the OSM knowledge graph entities to Wikidata(Semantic + Geo-spatial)
  4. Spatial Link Prediction
        -> USLP discovers relationships between entities using tri‑space scoring (geo + name + class)
        -> Augmented Data sources inlcude Google Places API and toronto-data(Django App in backend) or any appropriate open source data 
  5. Learned Layer
        -> The embeddings saved for each entity are used to train graph representation learning models.
          ==> FastText: 300D semantic embeddings via weighted average of tag embeddings (entity-local, no retraining)
          ==> DeepWalk: 100D spatial embeddings via weighted random walks on k-NN graphs (IDW edge weights)
          ==> SBERT: 384D→300D projection head for knowledge distillation from FastText space
            -> Region-specific projection heads (country/subgraph level) for local adaptation
            -> Trained via knowledge distillation using (SBERT embedding, FastText embedding) pairs   

## Quickstart

# Build images
docker compose -f docker-compose.yml -f compose.override.yml build

# Start databases and Redis
docker compose -f docker-compose.yml -f compose.override.yml up -d postgres-default postgres-vectors redis

# Start backend API + Celery worker
docker compose -f docker-compose.yml -f compose.override.yml up -d backend worker

# Start Frontend
```bash
cd frontend-v3
npx vite --port 5173 --host 0.0.0.0
```

## ML System Design Principles
<!-- TODO remove this from the README.md -->
<!-- TODO: site these references and take notes-->
<!-- Linear Algebra: https://github.com/mikexcohen/LinAlg4DataScience -->
<!--   ==> Chapter 4: Vector Applications how PG_Vector is used as a mechanism for similarity search -->
<!--   ==> Chapter 7: Multivariate Data Covariance Matrix is used in Statistical Analysis to understand relationships of the noisy/homogeneous OSM Data  -->
<!--   ==> Chapter 13 and 15: Eigendecomposition, PCA and Singular Value Decomposition (SVD) will be used in the Agentic Tool Calls -->
<!--   ==> Math Concepts to cover that are implementing: Sharding the DB, -->
<!-- Hands On Machine Learning with PyTorch: https://github.com/ageron/handson-mlp -->
<!--   ==> TODO: Go through maths in in the steps of the pipeline that uses Scientific Computing concepts -->
<!-- Practical Statistics for Data Statistics: https://github.com/gedeck/practical-statistics-for-data-scientists/tree/master/python/notebooks -->
<!--   ==> TODO: Go through how Statistical Analysis concepts that are used (Mostly ETL and data-validation) -->
<!-- Designing Machine Learning Systems by Chip Huyen -->
<!--   ==> Chapter 3: How this project uses Batch Processing and Parallel Processing concepts -->
<!--   ==> Chapter 8: Monitoring and Observability concepts -->
<!--   ==> Chapter 9: Using the Rejected Links with SSLP/Data Augementation to do Continual Learning -->
<!--   ==> Chapter 10: MLOps concepts that we use to develop on The RTX Cards but deploy on the K80 -->

## Chip Huyen’s Designing Machine Learning Systems principles:
  Pre‑compute expensive steps
    -> Planet snapshots, continent extracts, and GeoVectors embeddings are computed once and reused.  
    -> This turns most workloads into read‑heavy operations instead of re‑processing the planet for each run.
    -> Idempotency pattern for OSM PBF files produced via Osmium tool

  Batch and parallel processing  
    -> Work is batched per country and per subgraph.  
    -> Celery runs many tasks in parallel, especially for large countries, to speed up processing.

  Structured logging and observability  
    -> Each Celery task logs inputs, outputs, and timing.  
    -> Per‑stage metrics (counts, durations, basic quality checks) make it easier to understand where time and failures occur.

  Config‑driven behavior  
    -> OSM-Wikidata pipeline primitives are processed during the initialization phase based on configurations (e.g., country-specific overrides, embeddings available)
    -> Paths, thresholds (e.g., USLP, entropy), and country‑specific overrides are defined in configuration and JSON files, not hard‑coded in the codebase.
    -> OSM uses idomatic patterns and conventions to make the code more maintainable and easier to understand

  Reproducibility  
    -> A pipeline run is defined by code version + configuration + input paths.  
    -> You can repeat a run with the same settings to reproduce results.

  Clear interfaces between stages  
    -> Stages communicate via well‑defined artifacts (snapshots, TSVs, database tables).  
    -> You can improve a model inside one stage as long as it respects the same input/output format.

  Unified open‑source storage stack (PostgreSQL + pgvector + Redis)
    ->  Postgres is used as the main database for application state + Enforce OSM-Wikidata Hierarchy constraints
    ->  PostGIS‑enabled database for spatial data used in the stages of the pipeline  
    ->  pgvector‑backed database for embeddings and vector similarity search.  
    ->  Redis is used as the message broker and cache for Celery and the WebSocket channel layer.  
    ->  Redis is used to store the WorldKG Ontology and other metadata.

## Hands On Machine Learning with PyTorch and Sci-Kit Learn
Valdiate the construction of the pickle files
  -> BallTree indexing for efficient nearest neighbor queries in embedding space
  -> IDW interpolation to reconstruct embeddings from neighbors (inverse-distance weighting)

Visualize the drift between the original embeddings from the pre-trained model vs the interpolated embeddings
  -> PCA visualization of local neighborhoods to understand embedding structure
  -> t-SNE global projection to identify macroscopic clusters in the embedding space
  -> Distance distribution analysis (1st neighbor, k-th neighbor) to assess sparsity and clustering

Graph Structural Drift Analysis
  -> k-NN graph construction from spatial embeddings via BallTree for structural analysis
  -> Degree distribution tracking via histograms and KL divergence
  -> Clustering coefficient monitoring to detect topological changes
  -> Connected component size analysis for graph fragmentation detection
  -> 3-sigma drift thresholds to flag anomalous snapshots automatically
  -> KS tests for distributional comparison between baseline and current snapshots

Batch processing and pre‑compute are used heavily to make good use of available hardware
  -> Pre‑compute configs and primitives - WorldKG primitives (see `docs/Schematics/WorkKG_Primities.md`) are generated once and reused.
  -> Multi‑core processing with Osmium - Osmium‑tool is used to parallelize low‑level extraction work.
  -> Batch processing - Vector generation and spatial link prediction are run in batches rather than one entity at a time.
  -> Fan‑out processing for subgraphs(wikidata admin=2) - Large countries are split into subgraphs (administrative subdivisions) so work can be processed in parallel.

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
