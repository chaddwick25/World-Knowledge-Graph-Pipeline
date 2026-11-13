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

**Step 3: IGEA - iterative geographic entity alignment** → `OsmEntity.wikidata_uri` populated for aligned entities.

**Step 4: USLP - unsupervised spatial link prediction** (this doc):
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
    # Precision 1 - country/continent level (~5,000 km)
    'isIn':           1,
    'addrPlace':      1,
    'isInContinent':  1,
    'country':        1,
    'isInCountry':    1,
    'addrCountry':    1,
    'capitalCity':    1,

    # Precision 3 - state/county/district level (~156 km)
    'addrState':      3,
    'addrDistrict':   3,
    'addrProvince':   3,
    'isInCounty':     3,
    'isInState':      3,
    'isInDistrict':   3,

    # Precision 4 - local level (~39 km)
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
| IE | 507,985 | - | 11 | densest |
| KR | 63,021 | - | 5 | |
| BZ | 20,548 | 2,358 (avg 8.7 links/head, max 69) | 6 | ~11.5% of named entities are heads |
| JM | 482 | - | 3 | ⚠️ sparse, consumers must fall back |

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

The USLP-weighted question-generation design proposes re-weighting
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

---

## The candidate pool (corrected 2026-11-11)

**An earlier revision of this section claimed the pool requires `gv_nle_embedding`.
That was wrong.** No embedding filter exists in the pool query.

```
pool = geom IS NOT NULL
     + snapshot_id partition
     + country_code partition        <- NOT a tag match (see below)
     + optional polygon
```

USLP scores on **`gv_tags_embedding`**, which Step 1 writes for every entity.
`gv_nle_embedding` is a DeepWalk graph embedding and is not one of the paper's
three spaces; it is not a USLP input. There is no dependency on Step 5.

### The country tag filter: a second `addr:country` mistake

`load_candidate_pool_from_db` contains:

```python
if country and not polygon_wkt:
    country_keys = ...         # matches ISO / name / QID against TAGS
    qs = qs.filter(...)
```

A tag is being used as a country assignment when the table is already partitioned
by `country_code`. Measured on JM @ `2026_09_30`:

```
geometry rows                     31,048
after the country tag filter          96     <- the addr:country='JM' count
```

The same filter on JM @ `2025_12_31`: 1,113,365 entities carry a geometry but
only 384 carry `addr:country='JM'`, so the pool was cut to 0.3 percent of the
country. `addr:country` is a rare address tag, not a country assignment.

A pool of 96 yields almost no heads, nothing passes the 0.7 gate, and the GPU
path returned `[]` with no log and no exception. The step reported success. An
empty pool now logs at ERROR with its country/snapshot/polygon scope.

This is the **same mistake that starved IGEA**, in a different function, fixed
there and not here. The table is partitioned by `country_code`, so the partition
key already scopes the pool exactly and the tag check added nothing but loss.
Country and polygon are independent filters now: a country-scoped run may also
pass a polygon without the country filter disappearing. The intended query uses
the partition key:

```python
if country:      qs = qs.filter(country_code=country)
if polygon_wkt:  qs = qs.filter(geom__within=poly_geom)
```

The same tag filter also starved the head set. `predict_spatial_links` applied
it a second time when loading head entities (`--country` without `--poly-file`).
On JM @ `2025_12_31`, only 384 of 1,113,365 geometry rows carry
`addr:country='JM'`, so the tag check kept 0.03 percent of the country:

```
JM @ 2025_12_31, heads with a spatial literal tag
  country_code partition + tag key existence      5,164
  plus addr:country / is_in:country / is_in tag        384
```

The table is partitioned by `country_code`, so the partition key scopes the
heads exactly and the tag check added nothing but loss. Both queries now use
`country_code` and treat country and polygon as independent filters.

### Status

```
identified     yes - the tag filter is the cause of the 96-row pool
fixed          yes - 2026-11-11, pool and head queries use country_code
verified       yes - 2026-11-11, JM @ 2026_11_02 end-to-end
```

Verified run `8f8d4391`, full JM chain:

```
Step 1  embed_osm_entities          399,007 ms
Step 2  harvest_wikidata            110,349 ms
Step 3  run_igea                     32,538 ms
Step 4  predict_spatial_links        62,129 ms   on the k80
Step 5  train_gv_nle                213,252 ms
Step 5.7 graph_spectral_analysis    156,460 ms
Step 5.8 generate_sample_questions        6 ms
Step 6  mark_search_ready            52,129 ms
```

Step 4 on the k80:

```
USLP: Pool: 28,930 candidates (country_code=JM, snapshot=2026_11_02, polygon=no, limit=1720000)
TorchUSLP: Scored 49,100 links from 709 head entities (3,746 accepted, 45,354 rejected)
SpatialLinkPredictionService: persisted 3,746 accepted, 45,354 rejected
```

Rows in the k80 vectors DB for `2026_11_02`:

```
igea_spatial_triplet_score           3,799
igea_spatial_triplet_score_rejected  45,352
```

The chain advanced on the remote result: Step 3 completed at `10:29:01`, the k80
ran Step 4 from `10:29:02` to `10:30:04`, and Step 5 started at `10:30:04`. A
standalone `--stage 4` run (`e5c5e0dc`) produced the same 3,746 / 45,354 split.

One gap remains: the execution lines are written to the k80's per-run log, not
the dispatcher's copy. That is the remaining log-forwarding gap.

The fix:

```
load_candidate_pool_from_db
  geom IS NOT NULL
+ snapshot_id partition
+ country_code partition   (resolve_iso_code, no tag matching)
+ optional polygon         (independent of country)

empty pool                 logger.error with country/snapshot/polygon scope
pool size                  logger.info "USLP: Pool: N candidates (...)"

TorchUSLP.predict_links_batched
  keeps top_k overall, persist_links splits accepted/rejected, so the
  rejected table is populated on the GPU path too
```

### Where USLP runs

USLP is a GPU stage. All four entry points route to the k80 worker by task
name (`pipeline/task_routing.py` -> `K80_GPU_QUEUE`):

```
step_4_predict_spatial_links            canvas + run_pipeline --stage 4
sub_run_subgraph_uslp                   subgraph fan-out
step_4b_finalize_subgraph_uslp          chord callback
igea.tasks.run_uslp_for_subgraph_batch  API batch
```

The backend and worker services set `K80_GPU_QUEUE=k80-gpu` by default
(docker-compose.yml), and the k80 worker consumes `-Q k80-gpu`. When
`uslp.use_gpu=true` and the executing host has no CUDA, the step fails instead
of falling back to CPU: the two paths produced different link counts and only
the CPU path wrote rejected rows, so a misrouted step looked like a data defect.
`--wait` no longer forces eager execution when the queue is set, so
`--stage 4 --wait` dispatches to the k80 and blocks on the result.

The k80 image bakes the code (no bind mount), so a USLP change is not live
there until the image is rebuilt:

```bash
# on the k80, from a synced copy of backend/
docker build -f Dockerfile.k80-worker -t worldkg/k80-worker:1.0 .
cd /opt/worldkg-deploy && docker compose -f compose.worker.yml up -d
```

**Measured on JM @ `2025_12_31`:**

```
end-to-end run                 accepted 0     rejected 0
re-run after Step 5 completed  accepted 475   rejected 4,325
```

The rejected count moving from 0 to 4,325 is the proof that candidates were
generated and scored. An empty rejected table always means selection failed, not
that a threshold rejected everything.

### Reproducing the old baseline

| Relation | Old DB | New DB | Precision |
|---|---|---|---|
| `addrCity` | 362 | 349 | 4 |
| `addrState` | 76 | 76 | 3 |
| `addrPlace` | 50 | 50 | 1 |
| **Total** | **488** | **475** | |

97.3 percent agreement with identical relation mix and precision assignment, so
the implementation is faithful once the pool is populated.

### The log line that used to lie

The step used to log `N links accepted` where N was the number **scored**, not
accepted. On JM it logged 4,800 while 475 reached `igea_spatial_triplet_score`.
Since 2026-11-11 the command logs `N links scored, M accepted (threshold=...)`,
and `TorchUSLP` plus the command log the accepted/rejected split explicitly.
Still read the table for the authoritative count.

### Why moving USLP after Step 5 does not help

USLP does not read `gv_nle_embedding`, so its canvas position relative to Step 5
is irrelevant to pool population. The pool is geometry plus partition keys; the
scoring uses `gv_tags_embedding` (space 2, name/semantic) which Step 1 writes.
The earlier suggestion to move the stage or to fall back to `gv_tags_embedding`
addressed a dependency that does not exist.

## Divergence from the reference implementation

`gkmn21/SSLPandUSLP`, `USLP/USLP_main.py`:

| Aspect | Reference | Ours |
|---|---|---|
| Relation set | 14 relations | 22 relations |
| Spatial filter | cluster normalisation only | normalisation plus fixed radius |
| Radius per precision | none | 5000 / 156 / 39 km |
| Score combination | `np.sum()`, unweighted | weighted by `w_geo`, `w_name`, `w_topo` |
| Threshold meaning | sum of 3 scores >= 0.7 | weighted sum >= 0.7 |

`addrCity` is absent from the reference `geohash_precision_dictionary` entirely,
yet it produced 362 of the 488 historical JM links (74 percent). Our extension of
the relation set is what generated most of the output.

Because a weighted combination can never exceed the unweighted sum, our
`normalized_score` is not on the same scale as the paper's and the 0.7 threshold
means something different in each.


---

## The scoring flow, visualised

Redrawn from [USLP]. Divergences from the reference are annotated inline
and tabulated in PAPER_FIGURE_MAP.md.

## 1. The shape of the problem

A USLP head is an entity that names a place in a tag but has no link to the
entity that place actually is. The paper's own example:

> The entity representing Leicester carries the relations `wkgs:isInCountry` and
> `wkgs:isInCounty`, holding the literals "United Kingdom" and "Leicestershire",
> while lacking links to the entities those literals name.

So the task is: given a head with a literal, find the tail entity that literal
refers to, and decide whether the match is good enough to assert.

```
  head entity                                  candidate tail
  +---------------------------+                +---------------------------+
  | node 773213335            |                | node 4012862325           |
  | addr:city = "Kingston"    |  - - - - - ->  | name   = "Kingston"       |
  | geom = (17.99, -76.79)    |                | geom = (17.98, -76.80)    |
  | no link to Kingston       |                | is Kingston               |
  +---------------------------+                +---------------------------+
        |                                              ^
        |                                              |
        +---- score it on three axes, accept if good --+
```

---

## 2. The full flow

```
                        head entity (osm_type, osm_id, tags, geom)
                                        |
                                        v
              +-------------------------------------------------+
              | 1. EXTRACT SPATIAL LITERALS                     |
              |    for each tag in SPATIAL_LITERAL_TAGS:        |
              |      addr:city   -> relation addrCity           |
              |      addr:state  -> relation addrState          |
              |      is_in       -> relation isIn               |
              |    value becomes the LITERAL string             |
              +-------------------------------------------------+
                                        |
                                        v
              +-------------------------------------------------+
              | 2. PICK PRECISION FOR THE RELATION              |
              |    RELATION_GEOHASH_PRECISION[relation]         |
              |      precision 1  country / continent           |
              |      precision 3  state / county / district     |
              |      precision 4  suburb / hamlet / locality    |
              +-------------------------------------------------+
                                        |
                                        v
              +-------------------------------------------------+
              | 3. FIND CANDIDATE TAILS                         |
              |    BallTree radius query around the head        |
              |    radius from precision (ours; see section 5)  |
              +-------------------------------------------------+
                                        |
                                        v
              +-------------------------------------------------+
              | 4. SCORE EACH CANDIDATE ON THREE AXES           |
              |                                                 |
              |   space1  spatial      geohash cluster similarity|
              |   space2  semantic     cosine(literal, label)   |
              |   space3  structural   type + relation embedding|
              |   space4  optional     SBERT tag-value (ablation)|
              +-------------------------------------------------+
                                        |
                                        v
              +-------------------------------------------------+
              | 5. COMBINE AND THRESHOLD                        |
              |    reference:  sum(scores)  >= 0.7              |
              |    ours:       weighted sum >= 0.7              |
              +-------------------------------------------------+
                                        |
                        +---------------+---------------+
                        |                               |
                        v                               v
              accepted: written to               rejected: written to
              igea_spatial_triplet_score          ..._score_rejected
```

**The diagnostic that matters:** an empty *rejected* table means step 3 produced
no candidates at all. A threshold problem rejects; a selection problem produces
nothing. Those are different failures and they look identical in the accepted
count.

---

## 3. What the precision actually does

The precision is **not** a fixed distance cutoff in the reference. It selects a
set of geohash cluster centres, and the spatial score is normalised against the
distances between those centres as they actually occur in the data.

```
  precision 1  (country / continent)          precision 4  (locality)

  +--------+     +--------+                   +--+  +--+  +--+
  | C1     |     | C2     |                   |c1|  |c2|  |c3|
  |        |     |        |                   +--+  +--+  +--+
  |   *    |     |   *    |                    *     *     *
  +--------+     +--------+                   (head sits inside one cell)

  cluster centres are thousands of km apart   centres are a few km apart

  score = 1 - ( d(head, tail) / d_max_for_this_cluster )
```

`d_max_for_this_cluster` is the distance from the cluster centre to its farthest
sibling centre. So the same physical distance scores differently at precision 1
and precision 4, because the administrative level it represents is different.

```
  same 30 km separation, two different relations:

  addrState (precision 3)              addrCity (precision 4)
  |<---------- 156 km ---------->|      |<---- 39 km ---->|
       30 km is small                      30 km is large
       -> high geo score                   -> low geo score
```

This is the criterion you were describing: **the scale is set by what the label
represents, not by a constant.** It is the same principle as normalising a
similarity score against its own distribution rather than comparing it to a
hardcoded number.

---

## 4. The three spaces

```
  space1  SPATIAL
          head geohash at the relation's precision
          vs precomputed cluster-centre similarity matrix
          -> how plausible is this distance for this relation level

  space2  SEMANTIC
          cosine( embedding(literal string), embedding(candidate labelNameEn) )
          -> does "Kingston" look like the candidate's name

  space3  STRUCTURAL
          type + relation embeddings
          -> is this candidate the right KIND of thing for this relation

  space4  OPTIONAL (ablation only)
          SBERT over the entity's tag values
```

The reference sums them:

```python
tail_scores = np.sum(scores_list)     # USLP_main.py
```

There is no weighting in the paper's implementation. Each space contributes
raw, and the 0.7 threshold applies to the sum of three (or four) values.

---

## 5. Where our implementation differs from the reference

| Aspect | Reference | Ours |
|---|---|---|
| Relation set | 14 relations | 22 relations |
| Spatial filter | cluster normalisation only | normalisation **plus** fixed radius |
| Radius per precision | none | 5000 / 156 / 39 km |
| Score combination | `np.sum()`, unweighted | weighted by `w_geo`, `w_name`, `w_topo` |
| Threshold meaning | sum of 3 scores >= 0.7 | weighted sum >= 0.7 |

### The relation set

The reference dictionary covers 14 relations and **does not include `addrCity`**:

```
reference:  isIn, addrPlace, isInContinent, country, isInCountry, addrCountry,
            capitalCity, addrState, addrDistrict, addrProvince, isInCounty,
            addrSubdistrict, addrSuburb, addrHamlet

ours adds:  addrCity, addrVillage, addrTown, addrNeighbour, isInState,
            isInDistrict, addrPlace(4)
```

`addrCity` is the notable one: it is absent from the paper's map, yet it
accounted for **362 of the 488 links** the old database produced for JM (74%).
Our extension of the relation set is what generated most of the historical
output.

### The radius

The reference lets the cluster normalisation decide plausibility. We query a
BallTree with a hard radius first, so a candidate just outside 39 km at
precision 4 is discarded before it is ever scored. That is a source of false
negatives the paper does not have.

### The weighting

A weighted combination can never exceed the unweighted sum, so `normalized_score`
is not on the same scale as the paper's. The 0.7 threshold therefore means
something different in each implementation, and any calibration must account for
that before comparing against published results.

---

