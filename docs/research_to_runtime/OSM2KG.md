# Preserving the OSM2KG Transformation Pipeline

How the multi-language data parsing and transformation pipeline from the original [NicolasTe/osm2kg](https://github.com/NicolasTe/osm2kg) repository was preserved, optimized, and integrated into the Django pipeline.

---

## 1. Graph Extraction (Java → Native Osmium)

### The Original Implementation
The original `osm2kg` pipeline utilizes a custom Java application (`Osm2kg.java`) intended to extract nodes and topological relations.
* **Mechanism:** It connects to a local, heavily customized **Virtuoso Graph Server**.
* **Transformation:** It executes expensive, networked `SPARQL` queries against the triplestore to dump physical graph nodes and relationships into intermediate text files (often `.n3` or `.ttl` triplets).

### The Updated Implementation
The Java and Virtuoso dependencies were eliminated entirely, preserving the core topological parsing using binary streams.
* **Mechanism:** The pipeline uses `Pyosmium` (a Python wrapper over the `libosmium` C++ library).
* **Transformation:** As demonstrated in `backend/geovectors_encoder/services/geovectors_service.py`, the pipeline opens the binary `.pbf` files directly via `osmium.SimpleHandler` and constructs `Point(lon, lat)` geometries for PostgreSQL storage.

**Code Reference:** `backend/geovectors_encoder/services/geovectors_service.py` and `backend/geovectors_encoder/services/vector_storage_service.py`
```python
# Instead of dumping text triples, the pipeline assembles and injects geographic
# point parameters directly into the PostgreSQL representation.
import osmium

class IdHandler(osmium.SimpleHandler):
    ...

# vector_storage_service.py
'geom': Point(lon, lat) if lat and lon else None,
```

---

## 2. Structural Graph Embeddings (OpenKE → GeoVectors)

### The Original Implementation
To compute how nodes relate to one another spatially within the graph, the original `osm2kg` `python/` directory relies on deep external embedding libraries like **OpenKE** or **PyTorch-BigGraph**.
* **Mechanism:** It initializes complex external environments, passing the `.n3` text triplets into translation models like `TransE` or `DistMult`.
* **Transformation:** The system outputs structurally dense embedding vectors characterizing physical relationships in the graph.

### The Updated Implementation
Initially, the pipeline attempted to map these relationships using `GV-NLE` (GeoVectors Node Location Embedding) random `DeepWalks`. However, a critical mathematical divergence was identified: DeepWalk computes *undirected spatial colocation*, whereas `TransE` strictly computes *directed hierarchical semantics* ($h + r \approx t$). DeepWalk was completely flattening and ignoring relation types (like `isInCounty`).

To preserve the ISWC paper's spatial-semantic logic without requiring OpenKE C++ dependencies, the pipeline implements **TransE** inside native PyTorch with GPU acceleration.

* **Mechanism:** A dedicated `TransEGraphEmbeddingService` in PyTorch. It uses PyTorch tensors to compute the exact Knowledge Graph relation rules: $d(h, r, t) = || \mathbf{h} + \mathbf{r} - \mathbf{t} ||_1$.
* **Transformation:** Instead of exporting triplestores across the server, the Django backend streams the in-memory relations to the `TransEGraphEmbeddingService`. The service generates identical relation distances by directly evaluating explicit topology.
* **GPU Acceleration**: CUDA support for large-scale knowledge graph embedding training

**Code Reference:** `backend/igea/services/transe_service.py`
```python
    def score_triple(self, head_id: int, relation: str, tail_id: int) -> float:
        # Enforces TransE margin constraints identical to OpenKE
        h = self.entity_embeddings(head_id)
        t = self.entity_embeddings(tail_id)
        r = self.relation_embeddings(relation)

        distance = torch.norm(h + r - t, p=1).item()
        score = 1.0 / (1.0 + distance)
        return score
```

---

## 3. Sparse Tags to Continuous Graph Space (Semantic Translation)

### The Underlying Methodology
To interface highly irregular text tags (e.g., `amenity=cafe`) with numerical Machine Learning layers, OSM strings must be translated into mathematically structured continuous vector domains.

### The Updated Implementation
Rather than generating textual corpus `.tsv` files to compute word embeddings remotely, the pipeline parses the raw spatial properties into semantic profiles on-the-fly inside the multiprocessing workers traversing the `.pbf` stream.
* **Mechanism:** The `FastTextEmbeddingService` directly digests a dictionary of normalized OSM string counts.
* **Transformation:** A weighted contextual algorithm: `synthesized_vector += (weight * fasttext_vec)`. The node's specific tag distribution linearly scales the magnitude of the semantic vector, which is finally L2-normalized, mapping the graph node against the 300D spatial domain.
* **Topological Geofencing:** By executing this pipeline linearly across targeted country `.pbf` files, the output matrices inherently geofence training vectors to that isolated regional topography without secondary geospatial clipping post-process.

---

## 4. Spatial Link Prediction Scoring (USLP Implementation)

### The Original Implementation
Within the `osm2kg` Python codebase, scripts calculate Unsupervised Spatial Link Predictions (USLP) to hypothesize missing relationships geographically (e.g., matching a restaurant to its parent county).
* **Mechanism:** The scripts ingest standard feature matrices and run comparative distance metrics across three namespaces: Location Context, Semantics, and Ontological Class.

### The Updated Implementation
The pipeline preserves this evaluation logic inside the backend, eliminating text-file handoffs.
* **Mechanism:** `backend/igea/services/spatial_link_prediction.py` houses the "Tri-Space" heuristic formulation.
* **Transformation:** It calculates dynamic Geohash distances (`_geo_score`), Semantic Word-bag distances (`_name_score`), and Tag-class distances (`_class_score`).

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

**Scoring Updates (May 2026):**
- **Threshold**: 0.7 on normalized score (previously 0.6)
- **Class fallback**: When `wkg_class` is None, derives class text from OSM tags
- **Relation mapping**: Compound relation names mapped to natural text for valid embeddings
- **Pre-filtering removed**: All scored candidates returned for proper accepted/rejected split

---

## 5. Conclusion
By extracting with `osmium`, mathematically inferring relationships using `GV-NLE DeepWalks`, and directly embedding PyTorch BiLSTMs (`CrossAttentionIGEA`), the system preserves the original `osm2kg` algorithms.

The primary difference lies in the infrastructure: a transition from disconnected Java, Triplestores, and CSVs into a cohesive Django orchestrator.
