# Preserving the USLP Model Architecture

How the geographic heuristics from the original [Unsupervised Spatial Link Prediction (USLP)](https://arxiv.org/abs/2304.09503) research paper were integrated into the Django pipeline.

---

## 1. The Tri-Space Evaluation Algorithm

### The Original Implementation
The core methodology for predicting unsupervised categorical relationship inferences (e.g. `OSM:Node(amenity=bank) -> isInCounty -> OSM:Relation(boundary=county)`) across large open graphs involves decomposing features across three discrete dimensions: Spatial (Geohash distance), Semantic (Name variations), and Topological Structure (Categorical semantics).

### The Updated Implementation
To preserve the paper's line-by-line algorithm, the pipeline reconstructs the `USLP` calculation inside the Django pipeline to evaluate relationships on the fly over local PostGIS datasets without requiring CSV matrices.
* **Mechanism:** `SpatialLinkPredictionService` explicitly defines the Tri-Space.
* **Transformation:** The total probability heuristic sums `_geo_score` (representing Geohash distance rules), `_name_score` (representing FastText token correlations), and the structurally enforced topological relation constraints. Evaluated distances are threshold-filtered matching the unsupervised extraction algorithm.
* **GPU Acceleration**: `GPUAcceleratedUSLP` extends the base service with PyTorch GPU acceleration for 10-20x speedup on large datasets. `TorchUSLP` extends it further with full-GPU batched scoring (precomputed geohash cluster centers, cached relation embeddings, `head_batch_size=256`) and is what `--gpu` / `uslp_use_gpu=True` actually instantiates.

**Database Storage:**
- **SpatialTripletScore Model**: Stores predicted links with decomposed scores (geo_score, name_score, topo_score, unnormalized_score, normalized_score) plus a `predicted` flag (True if normalized_score ≥ 0.7)
- **SpatialTripletScoreRejected Model**: Stores rejected links (score < threshold) separately for analysis. **Caveat:** only populated by the CPU and `GPUAcceleratedUSLP` paths: `TorchUSLP.predict_links_batched` (the pipeline default, `uslp_use_gpu=True`) filters below-threshold rows before persistence (see `torch_uslp_service.py` TODO), so this table stays empty on GPU pipeline runs
- **Subgraph Scoping**: Candidate pool filtered by subgraph polygon for accurate local link prediction
- **Geohash Precision**: Relation-specific precision stored for reproducibility
- **Country Name**: country_name field for filtering independent of snapshot_id (stores the `--country` value: the ISO code in pipeline runs)

**Score Normalization and Thresholding:**
The USLP algorithm (paper Section 3.3) computes scores as the sum of three components:
```
final_score = geo_score + name_score + class_score
```
- Paper does NOT normalize or divide by 3: the score is used purely for ranking (Hits@k, MRR)
- Paper does NOT apply `max(0, ...)`: cosine similarity ranges [-1, 1] and negative values
  carry information (relation and type are semantically opposite)
- Paper does NOT apply an acceptance threshold: USLP is an unsupervised ranking method

Implementation additions (for the persistence use case):
```
unnormalized_score = geo_score + name_score + class_score  ← same as paper
normalized_score   = unnormalized_score / 3.0              ← ÷3 preserves ranking
```
- name_score and class_score use raw cosine_similarity (no max(0, ...)): matching paper ✓
- Division by 3 was applied to make the threshold scale intuitive (0.0–1.0 instead of 0.0–3.0)
- This does NOT affect ranking (monotonic transformation)
- Threshold (0.7 on normalized = 2.1 on unnormalized) is an implementation addition
- Class score uses fallback to OSM tags when `wkg_class` is missing (e.g., `amenity=restaurant` → "restaurant")
- Relation names mapped to natural text for better embedding quality (e.g., `addrCity` → "city town")

**Key Fixes (May 2026):**
- **Pre-filtering removal**: All scored candidates now returned to `persist_links` for proper accepted/rejected split (CPU and `GPUAcceleratedUSLP` paths only: `TorchUSLP.predict_links_batched` still pre-filters by threshold and drops rejected rows)
- **Geo formula restoration**: `_geo_score` uses the paper's `1 - d/d_max` mapping (reference repo `dataprep_utils.py:83`) with per-tail-cluster `d_max` precomputed at pool-load time, clamped to [0, 1]. The prior `1 / (1 + dist_km)` mapping crushed the geo signal to near-zero. All three paths (CPU, GPU, TorchUSLP) now score geohash cluster-center distances identically. See `docs/plans/completed/USLP_GEO_SPACE_FORMULA_FIX.md`
- **GPU FastText compatibility**: GPU path uses `get_word_vector` with weighted averaging to match CPU path
- **Class embedding fallback**: When `wkg_class` is None, derives class text from OSM tags (priority: amenity, building, highway, leisure, landuse, natural, waterway, railway, aeroway, place, tourism, historic, shop, office, craft, sport)
- **Relation name mapping**: Compound OSM relation names (e.g., `addrCity`, `isInCountry`) mapped to natural text for valid FastText embeddings
- **Canonical bbox gating**: For country-level runs without an explicit polygon, `SpatialLinkPredictionService.load_candidate_pool_from_db()` now resolves a canonical bbox via `CountryPipelineProfile` + `populate_bbox_for_profile`. If no bbox can be resolved, it fails fast instead of silently loading planet-scale candidate sets.

**Code Reference:** `backend/igea/services/spatial_link_prediction.py`
```python
    def _total_score(self, head_lat: float, head_lon: float, head_osm_id: int,
                     relation: str, literal: str,
                     candidate: Dict) -> Tuple[float, float, float, float, float]:
        """Sum of three USLP spaces (equal weight, paper Section 3.3).

        Returns:
            (unnormalized_score, normalized_score, geo_score, name_score, class_score).
        """
        g = self._geo_score(...)
        n = self._name_score(...)

        # TransE explicitly calculates topological validity when available;
        # otherwise the paper's FastText class space is used.
        if self.transe_service:
            dist = self.transe_service.score_triple(...)
            c = 1.0 / (1.0 + dist)
        else:
            c = self._class_score(relation, candidate)

        unnormalized = g + n + c
        normalized = unnormalized / 3.0
        return unnormalized, normalized, g, n, c
```

---

## 2. Dynamic Spatial Range Resolutions (Geohash)

### The Original Implementation
The USLP paper highlights that relying on constant bounding-box calculations for linking features destroys logical clustering since relationships (`addrSuburb` vs `isInCountry`) possess wildly different spatial ranges. It uses Geohash string precision parsing.

### The Updated Implementation
The pipeline implements an algorithmic geohash encoder/decoder to mirror the paper's bit-level behavior and, in the production path, uses a C-accelerated `geohash2` backend with identical precision.

* **Mechanism:** Geohash-based bounding at relation-specific precision. Shorter geohashes correspond to larger spatial ranges: precision 1 ≈ 5,000 km (country/continent), precision 3 ≈ 156 km (state/county), precision 4 ≈ 39 km (city/suburb/locality). These are the only three tiers used (there is no precision 6–7 in the implementation).
* **Transformation:** Whether using the pure-Python encoder or the `geohash2` library, the USLP scoring logic always quantizes locations into geohash cells, computes cell centers, and applies the paper's `1 - d / d_max` mapping (reference repo `dataprep_utils.py:83`), where `d_max` is the tail cluster's max distance to any cluster center at that precision: the per-column max of the reference repo's distance matrix, precomputed at pool-load time by `_compute_d_max_per_precision`. Scores are clamped to `[0, 1]` since production heads may lie outside the tail cluster's span (unlike the reference repo's closed-world matrix lookup).

**Code Reference:** `backend/igea/services/spatial_link_prediction.py`
```python
    def _geo_score(self, head_lat, head_lon, tail_lat, tail_lon, relation):
        precision = RELATION_GEOHASH_PRECISION.get(relation, DEFAULT_GEOHASH_PRECISION)
        gh_h = geohash2.encode(head_lat, head_lon, precision=precision)
        gh_t = geohash2.encode(tail_lat, tail_lon, precision=precision)
        lat_h_str, lon_h_str = geohash2.decode(gh_h)
        lat_t_str, lon_t_str = geohash2.decode(gh_t)
        c_h = (float(lat_h_str), float(lon_h_str))
        c_t = (float(lat_t_str), float(lon_t_str))
        dist_km = haversine(c_h, c_t, unit=Unit.KILOMETERS)
        # Per-tail-cluster d_max (reference repo: per-column max of the
        # distance matrix); fallback covers degenerate single-cluster pools.
        d_max = _D_MAX_PER_CLUSTER.get(precision, {}).get(gh_t) \
            or _FALLBACK_D_MAX.get(precision, 39.0)
        sim = 1.0 - (dist_km / d_max)
        return max(0.0, min(1.0, sim))  # clamp: heads may lie outside the pool's span
```

---

## 3. Topologic Semantic Constraints (TransE)

### The Original Implementation
Attempting to predict directional links like `$head + $relation ≈ $tail` using symmetric adjacency clustering frameworks (like node2vec or DeepWalk) fundamentally destroys relationship directionality. The osm2kg pipeline inherently passes the generated semantic nodes through `OpenKE` to compute explicit `TransE` relational distances.

### The Updated Implementation
The pipeline synthesizes the `TransEGraphEmbeddingService` in PyTorch to correct this, eliminating `OpenKE` dependencies.
* **Mechanism:** A `$d = ||h + r - t||_1` Tensor evaluation framework processes the entities, yielding explicit topological bounds.
* **Transformation:** The pipeline applies the matrix algorithm over the candidate search array. Candidates that technically share spatial bounding boxes but violate the strict translational intent of the structural relationship are correctly filtered out, restoring algorithmic fidelity to the original implementation.

**Code Reference:** `backend/igea/services/transe_service.py`



## 4. Class Space Separation (Two-Axis Architecture)

### The Intentional Misalignment

The USLP class space (`_class_score`) uses the **paper's original relation classes** (`isInCountry`, `addrCity`, `addrSuburb`) as defined in:

- `papers/SSLPandUSLP-main/USLP/USLP_main.py:geohash_precision_dictionary` (14 relation types in the paper; the implementation extends this to 20: see the table below)
- `papers/SSLPandUSLP-main/USLP/approach_utils.py:space3_score()` (cosine between relation_emb and type_emb)

These are **spatial boundary operators**: they describe *where something is relative to something else* (100D GV-NLE space).

The WorldKG TTL ontology classes (`wkgs:Restaurant`, `wkgs:Highway`, `wkgs:Amenity`) are loaded from `WorldKG_Ontology.ttl` → Redis via `enrich_worldkg_classes --load-from-ttl`. These serve the **semantic axis**: they answer *what something is* (300D GV-Tags space).

This is not a bug: it is a **natural separation of concerns**:

| Axis | Classes | Source | Used By | Space |
|------|---------|--------|---------|-------|
| Semantic | wkgs:Restaurant, wkgs:Highway | WorldKG TTL (Zenodo) | Semantic search, query filtering | 300D GV-Tags |
| Spatial | isInCountry, addrCity, addrSuburb | USLP paper (Section 3.3) | Link prediction, boundary scoping | 100D GV-NLE |

### How the paper's class space is preserved

The paper's `space3_score` computes:
```python
cosine_similarity(relation_embedding[relation_uri], type_embedding[tail_type])
```

Where `relation_embedding` and `type_embedding` are both pre-trained FastText vectors stored in `type_and_relation_embeddings_file.h5`.

The implementation mirrors this exactly: just computed on-the-fly instead of pre-stored:
```python
emb_rel = FastText(RELATION_TO_NATURAL_TEXT[relation])  # "country" for isInCountry
emb_cls = FastText(wkg_class.replace('wkgs:', ''))      # "Country" for wkgs:Country
cosine = dot(emb_rel, emb_cls) / (norm(emb_rel) * norm(emb_cls))
```

### The relation vocabulary (paper's 14 preserved, extended to 20)

From `papers/SSLPandUSLP-main/USLP/USLP_main.py:120-135`, the relation-to-precision mapping. The implementation (`RELATION_GEOHASH_PRECISION`) preserves the paper's 14 relations and adds 6 more for OSM tags the paper's dataset did not cover (marked †):

| Relation | Geohash Precision | Spatial Range |
|----------|------------------|---------------|
| isIn, addrPlace, isInContinent, country, isInCountry, addrCountry, capitalCity | 1 | ~5,000 km (country/continent) |
| addrState, addrDistrict, addrProvince, isInCounty, isInState †, isInDistrict † | 3 | ~156 km (state/county) |
| addrSubdistrict, addrSuburb, addrHamlet, addrCity †, addrNeighbour †, addrVillage †, addrTown † | 4 | ~39 km (local) |

The same 3-tier radius (`{1: 5000, 3: 156, 4: 39}` km) is also used as the
BallTree / GPU spatial pre-filter at scoring time.

**Code Reference:** `backend/igea/services/spatial_link_prediction.py:RELATION_GEOHASH_PRECISION` (20 entries) and `RELATION_TO_NATURAL_TEXT` (19 entries: `country` has no natural-text mapping and falls back to the raw relation name)

### Cross-reference: where the TTL is used

The WorldKG TTL ontology is loaded via:
```bash
python manage.py enrich_worldkg_classes \
  --load-from-ttl /path/to/WorldKG_Ontolgy.ttl
```

And queried in semantic search (`backend/semantic_search/views_hybrid.py`):
```python
ontology = get_worldkg_ontology_service()
key = ontology.get_canonical_osm_key(wkg_class)        # e.g., "amenity"
val = ontology.get_canonical_osm_value(wkg_class)      # e.g., "restaurant"
```

The two never cross. The TTL drives query-time class filtering. The USLP relations drive link-prediction spatial boundary scoping.


For operational details (CLI flags, candidate pool scoping, GPU batching, acceptance gate, and evaluation metrics), see:

- `docs/plans/completed/USLP_IMPLEMENTATION.md`: full paper → repo → code traceability
- `docs/plans/completed/USLP_GEO_SPACE_FORMULA_FIX.md`: geo-space formula audit (`1 - d/d_max` restoration)
- `backend/igea/management/commands/predict_spatial_links.py`: CLI entry point (`--gpu`, `--poly-file`, `--snapshot-date`, …)
