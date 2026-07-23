# IGEA App — Iterative Geographic Entity Alignment & Spatial Link Prediction

Django app implementing IGEA and USLP algorithms for OSM-Wikidata entity alignment and spatial link prediction.

---

## Overview

This app houses the implementation of two research papers:

1. **IGEA**: Iterative Geographic Entity Alignment with Cross-Attention (Dsouza et al., ISWC 2023)
2. **USLP**: Unsupervised Spatial Link Prediction (Mann et al., ISWC 2023)

---

## Models

### `EntityAlignment`
Tracks OSM → Wikidata entity alignments.

**Key Fields**:
- `osm_type`, `osm_id` - OSM entity reference
- `wikidata_uri` - Aligned Wikidata entity
- `alignment_method` - SEED, COSINE, or CROSS_ATTN
- `confidence` - Alignment score (0.0-1.0)
- `iteration` - IGEA iteration number

### `SpatialTripletScore`
USLP tri-space scores for (head, relation, tail) triplets.

**Key Fields**:
- `head_osm_id`, `tail_osm_id` - Entity pair
- `relation` - Spatial relation (e.g., isInCounty)
- `geo_score`, `name_score`, `topo_score` - Tri-space scores
- `unnormalized_score` - Raw sum of scores (range [0, 3.0])
- `normalized_score` - Normalized score = unnormalized_score / 3.0 (range [0, 1.0])
- `predicted` - Accepted if normalized_score >= 0.7

---

## Services

### `IterativeEntityAlignmentService`
Simplified IGEA implementation with dual methods:

**Cosine Baseline**:
- Uses `gv_tags_embedding` (FastText 300D)
- F1 = 0.81-0.95 across countries
- No training required

**CrossAttentionIGEA** (optional):
- BiLSTM cross-attention neural network
- Learns cross-cultural semantic variations
- Requires pre-trained weights

**Usage**:
```python
from igea.services.iterative_alignment_service import IterativeEntityAlignmentService

service = IterativeEntityAlignmentService(
    max_iterations=3,
    threshold=0.6,
    max_distance_m=2500
)
service.load_wikidata_candidates_from_db(limit=100_000)
stats = service.run(country_code='DE')
```

### `SpatialLinkPredictionService`
USLP tri-space evaluation algorithm.

**Tri-Space Scoring**:
1. **Geo Score**: Geohash distance (precision by relation)
2. **Name Score**: FastText cosine similarity
3. **Topo Score**: TransE topological constraints

**Usage**:
```python
from igea.services.spatial_link_prediction import SpatialLinkPredictionService

service = SpatialLinkPredictionService()
service.load_candidates_from_db(wkg_class='wkgs:County', limit=10_000)

links = service.predict_links(
    head_osm_id=123456,
    head_lat=51.5074,
    head_lon=-0.1278,
    relation='isInCounty',
    literal='Leicestershire',
    top_k=10
)
```

### `CrossAttentionIGEA`
BiLSTM cross-attention neural model.

**Architecture**:
- Bidirectional LSTM (hidden_dim=150)
- Cross-attention mechanism
- Binary classifier

**Usage**:
```python
from igea.services.cross_attention_model import CrossAttentionIGEA

model = CrossAttentionIGEA(emb_dim=300, hidden_dim=150)
model.load_weights('/path/to/weights.pt')
model.eval()

# Forward pass
score = model(osm_seq, wd_seq)  # Returns alignment probability
```

### `TransEGraphEmbeddingService`
Native PyTorch TransE implementation.

**Formula**:
- Distance: `d = ||h + r - t||_1`
- Score: `c = 1.0 / (1.0 + d)`

**Usage**:
```python
from igea.services.transe_service import TransEGraphEmbeddingService

service = TransEGraphEmbeddingService()
service.load_model('/path/to/model.pt', entity2id, rel2id)

score = service.score_triple(
    head_id=123,
    relation='isInCounty',
    tail_id=456
)
```

---

## Management Commands

### `run_igea_alignment`
Run IGEA iterative entity alignment.

**Usage**:
```bash
# Basic usage
poetry run python manage.py run_igea_alignment

# With options
poetry run python manage.py run_igea_alignment \
    --country DE \
    --method cosine \
    --iterations 3 \
    --threshold 0.6 \
    --dry-run
```

**Options**:
- `--country` - ISO country code (e.g., DE, GB, US)
- `--method` - cosine or cross_attention (default: cosine)
- `--iterations` - Max iterations (default: 3)
- `--threshold` - Min confidence (default: 0.6)
- `--max-distance` - Max spatial distance in meters (default: 2500)
- `--dry-run` - Preview without writing to database

### `predict_spatial_links`
Predict spatial entity links using USLP.

**Usage**:
```bash
# Basic usage
poetry run python manage.py predict_spatial_links

# With options
poetry run python manage.py predict_spatial_links \
    --threshold 0.7 \
    --top-k 10 \
    --limit 50000 \
    --dry-run
```

**Options**:
- `--threshold` - Min total score (default: 0.6)
- `--top-k` - Top K candidates per head (default: 5)
- `--limit` - Max entities to process
- `--snapshot-id` - Temporal snapshot UUID
- `--dry-run` - Preview without writing to database

---

## API Endpoints

All endpoints mounted at `/api/igea/`:

### IGEA Alignment

**POST `/api/igea/align/`**
Run IGEA alignment.

**Request**:
```json
{
    "country_code": "DE",
    "method": "cosine",
    "iterations": 3,
    "threshold": 0.6,
    "dry_run": false
}
```

**GET `/api/igea/align/status/`**
Get alignment statistics.

**GET `/api/igea/align/results/?osm_id=123&osm_type=node`**
Query alignment results.

### USLP Triplet Scoring

**POST `/api/igea/triplets/predict/`**
Run USLP prediction.

**Request**:
```json
{
    "head_osm_id": 123,
    "relation": "isInCounty",
    "limit": 10
}
```

**POST `/api/igea/triplets/score/`**
Score specific triplet.

**Request**:
```json
{
    "head_osm_id": 123,
    "relation": "isInCounty",
    "tail_osm_id": 456
}
```

**Response**:
```json
{
    "geo_score": 0.3,
    "name_score": 0.4,
    "topo_score": 0.2,
    "unnormalized_score": 0.9,
    "normalized_score": 0.3,
    "predicted": false
}
```

**POST `/api/igea/triplets/validate/`**
Validate triplet using TransE.

---

## Testing

### Mathematical Invariant Tests

Run comprehensive mathematical property tests:

```bash
poetry run pytest igea/tests/test_invariants.py -v
```

**Test Coverage**:
- Cosine similarity (8 tests)
- Geohash encoding (6 tests)
- Haversine distance (5 tests)
- USLP relation precision (3 tests)
- TransE scoring (4 tests)
- IGEA/USLP thresholds (5 tests)

**Total**: 31 tests validating mathematical correctness

---

## Mathematical Foundations

### IGEA Parameters (from paper)
- **Alignment threshold**: 0.6 (Fig. 5: optimal across countries)
- **Max distance**: 2500m (Section 3.2: validated value)
- **Max iterations**: 3 (Fig. 3: performance peaks)

### USLP Parameters (from paper)
- **Acceptance threshold**: 0.6 (Section 3.3)
- **Geohash precision**: Dynamic by relation (Table 1)
  - Continent: 3
  - Country: 4
  - County: 5
  - City: 6
  - Suburb: 7

### TransE Formula
```
d = ||h + r - t||_1  (L1 norm)
c = 1.0 / (1.0 + d)  (score conversion)
```

---

## Database Routing

Models in this app route to the **vectors** database (PostgreSQL with pgvector + PostGIS).

Configured in `backend/database_router.py`:
```python
route_app_labels = {
    'vectors', 'graph_analysis', 'semantic_search',
    'geovectors_encoder', 'worldkg_nca', 'igea'
}
```

---

## Dependencies

This app uses existing project dependencies:
- **PyTorch** - Neural models (CrossAttentionIGEA, TransE)
- **NumPy** - Cosine similarity calculations
- **Haversine** - Geographic distance calculations
- **Django REST Framework** - API endpoints

No additional packages required.

---

## References

1. Dsouza, A., Yu, R., Windoffer, N., & Demidova, E. (2023). *Iterative Geographic Entity Alignment with Cross-Attention*. ISWC 2023. https://arxiv.org/abs/2105.00847

2. Mann, Z., Dsouza, A., Yu, R., & Demidova, E. (2023). *Spatial Link Prediction with Spatial and Semantic Embeddings*. ISWC 2023 Best Paper. https://arxiv.org/abs/2304.09503

---

## Related Apps

- **worldkg_nca** - Entity storage, WorldKG ontology, NCA classification
- **geovectors_encoder** - Embedding generation (GV-Tags, GV-NLE)
- **semantic_search** - Query/retrieval, hybrid search

---

## License

Same as parent project.
