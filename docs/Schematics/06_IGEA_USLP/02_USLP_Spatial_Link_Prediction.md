# USLP: Unsupervised Spatial Link Prediction (Step 4)

> **Focus:** how USLP predicts spatial relationships between OSM entities
> using tri-space scoring (geo + name + class), geohash-based candidate
> pruning, and chunked bulk persistence. Covers the subgraph fan-out, the
> `SpatialTripletScore` model, and the SQL aggregate dashboard.
>
> **Key idea:** USLP discovers relationships like "this restaurant isIn
> this city" or "this suburb addrCity this town" by scoring OSM entity pairs
> across three spaces: geographic distance, name similarity, and class
> compatibility. Each relation type has a geohash precision that controls
> the spatial search radius. Country-level relations search 5000km, local
> relations search 39km. Accepted links (normalized_score ≥ 0.7) are
> persisted as `SpatialTripletScore` rows for the dashboard and downstream
> consumers.

---

## 1. Where USLP Sits in the Pipeline

**Step 3: IGEA — iterative geographic entity alignment** → `OsmEntity.wikidata_uri` populated for aligned entities.

**Step 4: USLP — unsupervised spatial link prediction** (this doc):
- Input: `OsmEntity` (vectors DB) with `geom`, `tags`, `wkg_class`
- Output: `SpatialTripletScore` (accepted links, `predicted=True`)
- Output: `SpatialTripletScoreRejected` (rejected links, below threshold)

**Step 5: Train GV-NLE (DeepWalk)** → uses all entities (aligned or not) for spatial graph construction.

USLP runs **independently** of IGEA. It is no longer gated on IGEA
acceptance count. The `total_accepted == 0` checks were removed from
`step_4_predict_spatial_links` and `_run_subgraph_uslp`.

**Source:** <ref_file file="backend/pipeline/tasks/country_pipeline_steps/step_4_uslp.py" />

---

## 2. The Tri-Space Scoring Architecture

**Head entity** (has a spatial literal tag), e.g. restaurant with
`tags={addr:city: "Havana"}`.

| Score space | Formula | Range |
|---|---|---|
| **GEO score** (geographic) | `1 - d/d_max`; d = haversine (head, tail); d_max = per-cluster max | [0, 1] |
| **NAME score** (semantic) | `cosine(FastText(literal), FastText(name))`; FT = FastText 300D cosine | [-1, 1] |
| **CLASS score** (ontological) | `cosine(FastText(relation), FastText(class))`; falls back to OSM tags if `wkg_class` NULL | [-1, 1] |

Combine:
1. `unnormalized = geo + name + class`, range [0, 3.0]
2. `normalized = unnormalized / 3.0`, range [0, 1.0]
3. `if normalized >= 0.7:` → `SpatialTripletScore` (`predicted=True`), else
   → `SpatialTripletScoreRejected`

**Source:** <ref_file file="backend/igea/services/spatial_link_prediction.py" />

---

## 3. The Three Score Spaces

### 3.1 Geo Score: Geographic Proximity

```python
geo_sim = 1 - d / d_max
```

- `d`: Haversine distance between head and tail entity
- `d_max`: Per-tail-cluster max distance (precomputed at pool load time)
- Range: [0, 1]. 1.0 means co-located, 0.0 means at max distance
- Uses geohash clustering to normalize distances per region

### 3.2 Name Score: Semantic Name Similarity

```python
name_sim = cosine(FastText(literal), FastText(candidate_name))
```

- `literal`: the spatial literal tag value (e.g. "Havana" from `addr:city`)
- `candidate_name`: the candidate entity's name tag
- FastText encodes both into 300D vectors, cosine similarity computed
- Range: [-1, 1], no clamping (preserves negative cosine for antonyms)
- Uses FastText `cc.en.300.bin` model (same as GV-Tags encoding)

### 3.3 Class Score: Ontological Compatibility

```python
class_sim = cosine(FastText(relation_text), FastText(candidate_class))
```

- `relation_text`: the relation name mapped to natural text
  (e.g. "isInCountry" → "country")
- `candidate_class`: the candidate's `wkg_class` (from Step 1 enrichment or
  NCA)
- Falls back to OSM tags when `wkg_class` is NULL
- Can use TransE distance if `transe_service` is available
- Range: [-1, 1]

### 3.4 Total Score

```python
unnormalized = geo + name + class    # Range: [0, 3.0]
normalized = unnormalized / 3.0      # Range: [0, 1.0]
```

Acceptance threshold: **0.7** (from `hyperparams.yaml`).

---

## 4. Geohash Precision: Relation-Specific Search Radius

**Source:** <ref_file file="backend/igea/services/spatial_link_prediction.py" lines="76-101" />

Each relation type has a geohash precision that controls the spatial search
radius. Shorter geohash = larger search area:

```python
RELATION_GEOHASH_PRECISION = {
    # Precision 1 — country/continent level (~5,000 km)
    'isIn':           1,
    'addrPlace':      1,
    'isInContinent':  1,
    'country':        1,
    'isInCountry':    1,
    'addrCountry':    1,
    'capitalCity':    1,

    # Precision 3 — state/county/district level (~156 km)
    'addrState':      3,
    'addrDistrict':   3,
    'addrProvince':   3,
    'isInCounty':     3,
    'isInState':      3,
    'isInDistrict':   3,

    # Precision 4 — local level (~39 km)
    'addrSubdistrict': 4,
    'addrSuburb':     4,
    'addrHamlet':     4,
    'addrCity':       4,
    'addrNeighbour':  4,
    'addrVillage':    4,
    'addrTown':       4,
}
DEFAULT_GEOHASH_PRECISION = 4
```

### 4.1 Radius Mapping

```python
radius_km = {1: 5000, 3: 156, 4: 39}.get(precision, 156)
```

| Precision | Cell Size | Radius | Relation Types |
|---|---|---|---|
| 1 | ~5,000 km | 5,000 km | isIn, isInCountry, addrCountry, capitalCity |
| 3 | ~156 km | 156 km | isInState, addrState, isInCounty, addrDistrict |
| 4 | ~39 km | 39 km | addrCity, addrSuburb, addrVillage, addrTown |

This aligns with the USLP reference implementation
(`SSLPandUSLP-main/USLP/USLP_main.py:120-135`).

### 4.2 Why Geohash Precision Matters

An "isInCountry" relation should search the entire country (~5000km); an
"addrSuburb" relation should only search the local area (~39km). The wrong
precision would either miss valid candidates (too narrow) or flood the
scorer with irrelevant candidates (too wide).

---

## 5. Subgraph Fan-Out and Rehydration

### 5.1 The Rehydration Problem

On a fresh DB, `has_subgraphs=False` at canvas dispatch time because subgraph
poly files have not been generated yet. The canvas chord for Step 4 was
built with this stale flag, so USLP would run at country level only
(7 entities → 39 links for Belize) instead of fanning out to subgraphs
(2,420 entities → 20,114 links).

### 5.2 The Fix: Self-Dispatch Inline

<ref_snippet file="backend/pipeline/tasks/country_pipeline_steps/step_4_uslp.py" lines="48-76" />

Step 4 now **rehydrates subgraphs from DB at task start**:
1. `CountryEnvelope.from_db(env.iso, snapshot_date=env.snapshot_date)`
2. If subgraphs are found after rehydration, the task **self-dispatches
   per-subgraph work inline** (loop over subgraphs, call
   `predict_spatial_links` for each)
3. The `rehydrated` flag distinguishes this path from the normal
   chord-driven path

### 5.3 Chord vs Inline Dispatch

| Path | When | How |
|---|---|---|
| Chord | `has_subgraphs=True` at canvas build time | `_run_subgraph_uslp.si()` per subgraph, chord callback aggregates |
| Inline | Subgraphs rehydrated at task start | Loop over subgraphs, call command directly |

The inline path is a **fresh-DB fix**. On a warm DB where subgraphs were
already generated, the chord path is used.

---

## 6. The SpatialTripletScore Model

**Source:** <ref_file file="backend/igea/models.py" />

```python
class SpatialTripletScore(models.Model):
    id = UUIDField(primary_key=True)
    head_osm_type = CharField(max_length=10)
    head_osm_id = BigIntegerField()
    tail_osm_type = CharField(max_length=10)
    tail_osm_id = BigIntegerField()
    relation = CharField(max_length=50)       # e.g. "isInCounty", "addrSuburb"
    geo_score = FloatField()                  # [0, 1]
    name_score = FloatField()                 # [-1, 1]
    topo_score = FloatField()                 # [-1, 1] (class score)
    unnormalized_score = FloatField()         # [0, 3.0]
    normalized_score = FloatField()           # [0, 1.0]
    predicted = BooleanField()                # True if normalized >= 0.7
    geohash_precision = IntegerField()        # 1, 3, or 4
    snapshot_id = CharField(max_length=20)    # partition key
    country_name = CharField(max_length=100)
    created_at = DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=['country_name', 'snapshot_id', 'predicted'],
                name='igea_triplet_csp_idx'
            ),
        ]
```

### 6.1 The Composite Index: `igea_triplet_csp_idx`

The composite index on `(country_name, snapshot_id, predicted)` enables
**Index Only Scan** for the dashboard's most common query:

```sql
SELECT ... FROM spatial_triplet_score
WHERE country_name = 'Cuba'
  AND snapshot_id = '2025_12_31'
  AND predicted = True;
```

Without the composite index, Postgres would do a bitmap scan + heap fetch
for each filter. With it, the index covers all three filter columns and
Postgres can satisfy the query from the index alone.

### 6.2 SpatialTripletScoreRejected

Same fields as `SpatialTripletScore` but without the `predicted` flag.
Stores links below the 0.7 threshold. Useful for debugging false negatives
and tuning the threshold.

---

## 7. Persistence: Chunked bulk_create

**Source:** `backend/igea/services/spatial_link_prediction.py` (`persist_links`)

```python
PERSIST_CHUNK = 5000

# Split into accepted/rejected buffers
if is_accepted:
    accepted_buf.append(SpatialTripletScore(**base_fields, predicted=True))
else:
    rejected_buf.append(SpatialTripletScoreRejected(**base_fields))

# Chunked bulk_create with ignore_conflicts=True
SpatialTripletScore.objects.bulk_create(accepted_buf, ignore_conflicts=True)
SpatialTripletScoreRejected.objects.bulk_create(rejected_buf, ignore_conflicts=True)
```

- **Chunk size**: 5000 rows per `bulk_create` call
- **`ignore_conflicts=True`**: idempotent. Re-running USLP for the same
  snapshot does not create duplicates
- **Split persistence**: accepted and rejected links go to separate tables
  (keeps the accepted table small for dashboard queries)

---

## 8. The Dashboard: SQL Aggregate with Case/When

**Source:** `backend/api/services/augmented_data_service.py` (`_aggregate_accepted_metrics`)

The USLP dashboard retrieves and aggregates metrics in a **single SQL
round-trip** using `aggregate()` + `Case/When` + `F()` expressions:

```python
result = accepted_qs.aggregate(
    total=Count('id'),
    geo_dominant=Count(
        Case(When(geo_score__gt=F('name_score') + F('topo_score'), then=1))
    ),
    name_dominant=Count(
        Case(When(name_score__gt=F('geo_score') + F('topo_score'), then=1))
    ),
    class_dominant=Count(
        Case(When(topo_score__gt=F('geo_score') + F('name_score'), then=1))
    ),
    # Histogram buckets
    b0=Count(Case(When(normalized_score__gte=0.0, normalized_score__lt=0.2, then=1))),
    b1=Count(Case(When(normalized_score__gte=0.2, normalized_score__lt=0.4, then=1))),
    b2=Count(Case(When(normalized_score__gte=0.4, normalized_score__lt=0.6, then=1))),
    b3=Count(Case(When(normalized_score__gte=0.6, normalized_score__lt=0.8, then=1))),
    b4=Count(Case(When(normalized_score__gte=0.8, then=1))),
    avg_confidence=Avg('normalized_score'),
)
```

### 8.1 Dominance Calculation

| Dominance | Condition | Meaning |
|---|---|---|
| geo_dominant | `geo_score > name_score + topo_score` | Geographic proximity drove the link |
| name_dominant | `name_score > geo_score + topo_score` | Name similarity drove the link |
| class_dominant | `topo_score > geo_score + name_score` | Class compatibility drove the link |
| mixed | `total - (geo + name + class)` | No single space dominated |

Using `F()` expressions keeps the arithmetic inside the SQL engine. No rows
are loaded into Python.

### 8.2 Histogram Buckets

| Bucket | Range | Label |
|---|---|---|
| b0 | [0.0, 0.2) | "0.0-0.2" |
| b1 | [0.2, 0.4) | "0.2-0.4" |
| b2 | [0.4, 0.6) | "0.4-0.6" |
| b3 | [0.6, 0.8) | "0.6-0.8" |
| b4 | [0.8, 1.0] | "0.8-1.0" |

The last bucket includes 1.0 (uses `>=` rather than a range).

---

## 9. Configuration

### 9.1 Hyperparams (from `hyperparams.yaml`)

**Source:** <ref_file file="backend/pipeline/hyperparams.yaml" />

```yaml
uslp:
  threshold: 0.7        # Acceptance threshold for normalized_score
  top_k: 50             # Top-k candidates per relation
  limit: 1720000        # Max candidates to load (tuned; paper default 200000)
  max_heads: 4385000    # Max head entities to process (tuned; paper default 50000)
  use_gpu: true         # GPU acceleration
  gpu_device: "cuda:0"  # GPU device
  use_fp64: false       # fp32 by default
```

The YAML is runtime truth; `settings.py` declares none of these knobs. The
frozen `ModelHyperparams` dataclass field defaults are the code-level fallback
when a YAML key is absent.

### 9.2 Hardcoded Constants (in `spatial_link_prediction.py`)

| Constant | Value | Purpose |
|---|---|---|
| `ACCEPTANCE_THRESHOLD` | 0.7 | Fallback if hyperparams not loaded |
| `DEFAULT_GEOHASH_PRECISION` | 4 | Default geohash precision (~39km) |
| `PERSIST_CHUNK` | 5000 | Rows per `bulk_create` call |

---

## 10. Data Flow

**Inputs:**
- `OsmEntity` (vectors DB): `geom` (PostGIS point, GiST indexed), `tags`
  (JSONB, including spatial literal tags like `addr:city`), `wkg_class`
  (from Step 1 enrichment or NCA), `gv_tags_embedding` (300D, for name
  score), `snapshot_id`, `country_code` (partition pruning)

**Process:**
1. Load head entities (entities with spatial literal tags), e.g. entities
   with `addr:city`, `addr:state`, `isIn` tags
2. Group heads by relation type: `isIn`, `addrCity`, `addrState`, etc.
3. For each head + relation:
   a. Determine geohash precision from the relation type
   b. Find candidate tail entities within the radius (BallTree or GiST)
   c. Score each candidate: geo + name + class
   d. Take top-k (50) candidates
   e. Filter by threshold (0.7)
4. Persist accepted/rejected links

**Outputs:**
- `SpatialTripletScore` (accepted, `predicted=True`): `head_osm_type`,
  `head_osm_id`, `tail_osm_type`, `tail_osm_id`, `relation`, `geo_score`,
  `name_score`, `topo_score`, `unnormalized_score`, `normalized_score`,
  `geohash_precision`, `snapshot_id`, `country_name`, `predicted = True`
- `SpatialTripletScoreRejected` (rejected, below threshold): same fields, no
  `predicted` flag

---

## 11. The Step 4b Chord Callback

**Source:** `backend/pipeline/tasks/country_pipeline_steps/step_4_uslp.py` (`step_4b_finalize_subgraph_uslp`)

When the chord path is used (subgraphs present at canvas build time):
1. Each `_run_subgraph_uslp.si()` task runs USLP for one subgraph
2. The chord callback `step_4b_finalize_subgraph_uslp` fires when all header
   tasks complete
3. The callback aggregates results and returns the config dict

When the inline path is used (subgraphs rehydrated at task start):
- The chord callback is a **no-op**. The task already ran USLP for all
  subgraphs inline.

---

## 12. Invariants

- USLP runs **independently** of IGEA, not gated on IGEA acceptance count.
- Tri-space scoring: `normalized = (geo + name + class) / 3.0`, range [0, 1].
- Acceptance threshold: **0.7** (from `hyperparams.yaml`).
- Each relation type has a geohash precision controlling the search radius
  (1 = ~5000km, 3 = ~156km, 4 = ~39km).
- Accepted links → `SpatialTripletScore` (`predicted=True`).
- Rejected links → `SpatialTripletScoreRejected`.
- Persistence is chunked (5000 rows) with `ignore_conflicts=True`
  (idempotent).
- The composite index `igea_triplet_csp_idx` on
  `(country_name, snapshot_id, predicted)` enables Index Only Scan for
  dashboard queries.
- Dashboard uses `aggregate()` + `Case/When` + `F()` for single-round-trip
  metrics (no rows loaded into Python).
- Subgraph rehydration: Step 4 rehydrates subgraphs from DB at task start
  if the canvas was built with stale `has_subgraphs=False`.

---

## 13. USLP Links as a Data Asset

Beyond the dashboard, the accepted links are a **queryable salience
signal**, a curated set of "locations that matter" produced by the pipeline
itself. This section documents what the links actually are, how much of
them exist, and the two ways the platform consumes them.

### 13.1 What a Link Means

Each accepted row is a **WorldKG object-property (containment/address)
triple**, not a generic "nearby" relation:

```
(head) --relation--> (tail)
 POI        contains/     region (city, county, province, country)
            addresses
```

Observed relations (from `RELATION_GEOHASH_PRECISION`, all
containment/address semantics): `addrCity` (dominant), `addrProvince`,
`isInCounty`, `addrPlace`, `isIn`, `addrTown`, `addrSubdistrict`,
`isInCountry`, `addrDistrict`, `addrCountry`, `addrSuburb`, `addrState`.

The **head** is a POI with a spatial literal (`addr:city` etc.) that USLP
confidently assigned to its region, i.e. a **named, semantically salient
place in a populated area**. The **tail** is the region (often an OSM
admin-boundary relation). Acceptance (`predicted=True`) means
`normalized_score ≥ 0.7`, the tri-space (geo + name + class) verdict.

### 13.2 Real Coverage (snapshot `2025_12_31`, `predicted=True`)

| Country | Links | Distinct heads | Relations | Notes |
|---|---|---|---|---|
| IE | 507,985 | — | 11 | densest |
| KR | 63,021 | — | 5 | |
| BZ | 20,548 | 2,358 (avg 8.7 links/head, max 69) | 6 | ~11.5% of named entities are heads |
| JM | 482 | — | 3 | ⚠️ sparse, consumers must fall back |

Heads are power-law distributed (a few entities have many links). Any
consumer weighting by link count should use rank/percentile-normalized
weights, not raw counts.

### 13.3 Consumption Pattern 1: Query-Time Salience Boost (implemented)

The executor already treats USLP as a salience signal at **query time**.
`_enrich_with_geo_and_uslp` (in `query_executor_service/geo_uslp.py`) adds a
`+0.5`-style boost to candidate results that are USLP predicted-link tails
for the query's head entity, then re-ranks. USLP already influences "which
results matter" for live queries.

### 13.4 Consumption Pattern 2: Training-Data Sampling Prior (planned)

`docs/plans/USLP_WEIGHTED_QUESTION_GENERATION_PLAN.md` proposes re-weighting
`generate_mapqa_training_data`'s entity pool by head salience (link count +
avg score, rank-normalized, blended with uniform at `alpha=0.5` by default)
so question generation focuses on the POIs USLP recognizes: the locations
where radius/nearest/adjacency questions have **non-trivial** answers.

**The prior-vs-truth boundary (critical):**

| Use | Verdict | Why |
|---|---|---|
| USLP as a **sampling prior** (which locations to focus on) | ✅ | Answers stay PostGIS-exact; USLP only re-weights selection |
| USLP link as the **answer** ("Which city is X in?" → USLP tail) | ❌ | **Pseudo-labeling**: USLP is a model prediction; using it as ground truth propagates its errors into whatever is trained on it (confirmation bias). The rejected-link table (`SpatialTripletScoreRejected`) would be actively misleading as truth |
| USLP proposes pairs, **PostGIS verifies** | ✅ hybrid (future) | "Which city is X in?" → USLP proposes (head, tail); `ST_Contains(tail_polygon, X)` decides the answer. Selection by USLP, truth by geometry |

This mirrors the generator's existing philosophy: USLP narrows *where to
look*; `ST_DWithin`/haversine/bearing compute *what's true*.
