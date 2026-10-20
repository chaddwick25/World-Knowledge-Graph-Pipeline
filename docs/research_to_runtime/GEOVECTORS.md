# Preserving the GeoVectors Model Architecture

How the semantic spatial matrices from the original [GeoVectors: A Linked Open Corpus of OpenStreetMap](https://hal.science/hal-03203496/document) research paper were integrated into the Django pipeline.

---

## 1. Local Node Text Embeddings (GV-Tags)

### The Original Implementation
The GeoVectors framework creates textual context for any OpenStreetMap node. Generating these vectors usually means loading large textual sequence files (`.tsv` dumps) into external C++ FastText bin pipelines, or running Python loops over JSON arrays to extract text embeddings from individual strings (e.g. converting the tag "bank" to a 300D embedding).

### The Updated Implementation
Rather than generating textual corpus `.tsv` files to compute strings remotely, the pipeline streams semantic properties on-the-fly inside worker processes that parse the `.pbf` stream directly.
* **Mechanism:** `FastTextEmbeddingService` calculates dense numeric features locally per object.
* **Transformation:** The exact mathematical scaling logic: `synthesized_vector += (weight * fasttext_vec)` based on normalized occurrences.
* **Output:** Bypasses file loads, feeding L2-normalized `gv_tags_embedding` Graph Space features directly into the `OsmEntity` model in the pgvector database.

**Database Storage:**
- **OsmEntity Model**: Stores `gv_tags_embedding` (300D, L2-normalized) and `gv_tags_version` for tracking
- **pgvector Database**: Dedicated vector database. Runtime search is exact cosine (`<=> ... + 0`), not HNSW. The `+ 0` defeats any HNSW index so PostgreSQL uses exact distance (verified 2026-09-09: no HNSW index on `gv_tags_embedding`; the 400D `static_embedding` HNSW path is opt-in via `use_ann` and not built)
- **Version Tracking**: Each embedding is tagged with a version string for reproducibility and regime change detection

**Code Reference:** `backend/semantic_search/services/fasttext_service.py`
```python
        for tag, count in tag_counts.items():
            vec = model.get_word_vector(tag)
            weight = count / total_weight
            synthesized_vector += (weight * vec)

        # L2 Normalization (Consistent with paper's Graph Space rules)
        norm = np.linalg.norm(synthesized_vector)
        if norm > 0:
            synthesized_vector = synthesized_vector / norm
```

---

## 2. Structural Node Location Embeddings (GV-NLE)

### The Original Implementation
Spatial embeddings encode physical geographic relationships into coordinate-free neural representations. The GeoVectors framework employs Unsupervised DeepWalk clustering on proximity arrays. Traditionally, this is triggered via large topological datasets passed to `Line` or `DeepWalk` algorithm infrastructures independently over cluster environments.

### The Updated Implementation
The pipeline abstracts the geographic walking procedure over isolated bounding boxes inside PostgreSQL, with optional GPU acceleration.
* **Mechanism:** A strictly localized _k_-NN structural network graph (`KNNGraphService`) bounded by a canonical country/subgraph bbox. For pipeline-managed runs, `train_gv_nle` resolves this bbox via `CountryPipelineProfile` + `populate_bbox_for_profile` (or an explicit `--poly-file`), then loads only buffered in-bounds entities from the `OsmEntity` vector DB. Parallel Skip-Gram walkers simulate probabilistic movements node-to-node.
* **Transformation:** Because the graph is defined on PostGIS vectors, the walker learns the probabilistic structural proximity embeddings (`gv_nle_embedding`) directly into the database, representing regional topography.
* **GPU Acceleration**: The default DeepWalk service is CPU `WeightedDeepWalkService`. The GPU path uses `PyGDeepWalkService` (PyTorch Geometric, CUDA support, 10-20x speedup over CPU). A legacy `GPUDeepWalkService` also exists. The GPU path is selected via the `--gpu` flag on `train_gv_nle`.

**Database Storage:**
- **OsmEntity Model**: Stores `gv_nle_embedding` (100D) and `gv_nle_trained` flag for tracking
- **Subgraph Support**: Hierarchical administrative divisions (e.g., Mozambique provinces) processed in parallel
- **IDW Fallback**: Inverse Distance Weighting for entities not in prior cycle's pickle

**Code Reference:** `backend/semantic_search/management/commands/train_gv_nle.py`
```python
# Extracts geographic proximity matrices representing spatial colocation maps,
# then random-walks them.
knn_service = KNNGraphService(k=k)
graph = knn_service.build_knn_graph(entities)

deepwalk = WeightedDeepWalkService(
    embedding_dim=embedding_dim,
    walk_length=walk_length,
    num_walks=num_walks,
    workers=workers
)
embeddings = deepwalk.train(graph, ...)
```

---

## 3. Inductive Damped Prediction

### The Original Implementation
A core limitation of standard neural walks is that node vectors are static. When new geometries are introduced, they lack representation unless the entire graph is retrained. GeoVectors resolves this using inductive "Damped Weight Iteration" based on Haversine distance falloffs.

### The Updated Implementation
The pipeline implements the distance decay mapping on the DB stream.
* **Mechanism:** A concurrent `InductiveSpatialService` utilizing `sklearn.neighbors.BallTree`.
* **Transformation:** At query/orchestration time, if no structural network node exists for an entity, the service pulls the 50 closest node representations via the Haversine metric space, applies the strict mathematical `w = max(1/ln(max(dist_km, 1.1)), e)` dampener, and produces provisional GV-NLE structural vectors. (PBF ingestion uses 20,000-entity batches via `parallel_upsert.chunk_size` in `pipeline/hyperparams.yaml`, not 50,000.)

**Code Reference:** `backend/semantic_search/services/inductive_spatial_service.py`
```python
def _damped_weight(dist_km: float) -> float:
    """w' = max(1/ln(max(d, 1.1)), e)  —  GeoVectors training formula."""
    d = max(dist_km, 1.1)
    return max(1.0 / np.log(d), np.e)
```
