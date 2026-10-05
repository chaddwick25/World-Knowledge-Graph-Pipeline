# MapQA Geographic Scoring and Routing: USLP, Radius Guards, Tag Filtering

> **Focus:** how the MapQA executor and semantic search view score
> candidates geographically, route radius-bearing queries away from
> graph-only templates, and filter by tag presence. These are the
> operational details behind the template executors in
> [01_MapQA_Parser_Executor.md](01_MapQA_Parser_Executor.md).
>
> **Key idea:** geographic scoring uses the **USLP formula** from Mann et
> al. 2023 §3.3 (geohash-quantized haversine, scale-aware normalization)
> rather than a uniform `1/(1+d)` decay. Radius-bearing queries are
> deterministically routed to `FILTER-AGGREGATE-MEASURE` regardless of the
> TF-IDF classifier's prediction, because the alternative
> (`PLACE-ATTRIBUTE-QUERY`) uses heat kernel diffusion, a graph
> connectivity measure that does not enforce geographic distance.

---

## 1. Geographic Scoring (USLP Formula)

**Source:** <ref_file file="backend/semantic_search/services/query_executor_service/" />

Both the MapQA executor and the semantic search view use the USLP
geographic space score from Mann et al. 2023 §3.3. The formula encodes
anchor and candidate coordinates to geohash at P4 precision (~39km
cells), computes haversine distance between cluster centers, and
normalizes by `d_max`:

```
geo_score = 1 - d_cluster / d_max
```

This replaces the old `1/(1+d_km)` decay, which treated all distances
uniformly. The USLP formula is scale-aware: entities in the same geohash
cell score 1.0, and scores decay linearly to 0.0 at `d_max`.

### 1.1 Template-Specific Behavior

| Template | Scoring | Rationale |
|---|---|---|
| `FILTER-AGGREGATE-MEASURE` (with explicit radius) | Raw haversine, `d_max` = user's radius | The user gave an exact distance, no geohash quantization |
| `OBJECT-FIELD-MEASURE` | Returns 0.0 | Distance IS the answer, not a ranking signal |
| All other templates | P4 geohash, `d_max` = 39km (P4 cell width) | Scale-aware default |

### 1.2 Fallback When No Precomputed Pool Exists

When no precomputed candidate pool is available, `d_max` falls back to
the P4 cell width (~39km). This keeps scoring well-defined even before
the first pipeline run materializes a pool.

---

## 2. USLP Signal Boost

When the USLP pipeline has predicted spatial links for the anchor entity
(`SpatialTripletScore.predicted=True`, normalized score >= 0.7), entities
that appear as predicted link tails receive a **+0.5 score boost**. This
connects the link prediction layer to search ranking. If USLP predicts
that the anchor has a spatial relationship with a candidate, that
candidate gets a small ranking advantage.

The boost is weighted low because USLP relations (`isInCounty`,
`addrSuburb`) don't directly map to proximity queries like "find bars
near a bus station". It is a complementary signal, not a primary ranker.

---

## 3. Radius Query Routing

**Source:** <ref_file file="backend/semantic_search/services/query_parser_service.py" />

Queries containing explicit radius language (`within Xkm of`, `within Xm
of`) are **deterministically routed** to `FILTER-AGGREGATE-MEASURE`
regardless of the TF-IDF classifier's prediction. This prevents
radius-bearing queries from being misrouted to `PLACE-ATTRIBUTE-QUERY`,
which uses heat kernel diffusion, a graph connectivity measure that does
not enforce geographic distance.

### 3.1 Safety Guard in PLACE-ATTRIBUTE-QUERY

A safety guard in the `PLACE-ATTRIBUTE-QUERY` executor also filters heat
kernel results by haversine distance when an `AMOUNT` concept is present.
Even if the parser misclassifies a radius query, geographically distant
entities are excluded. The guard is a defense-in-depth backstop behind
the deterministic routing override.

---

## 4. Tag Filtering

**Source:** <ref_file file="backend/semantic_search/services/query_executor_service/" />

The semantic search view filters the queryset to entities that have the
queried tag keys present (`tags__has_key`), even when exact tag matching
is disabled. This prevents entities without the tag from polluting
results. Querying `{"cuisine": "jamaican"}` no longer returns cafes with
no cuisine tag.

A **tag match boost** of +1.0 is added to the final score for each query
tag value that exactly matches the entity's tag value, ensuring
`cuisine=jamaican` ranks above `cuisine=indian` even when their FastText
embeddings are semantically similar.

---

## 5. Invariants

- Geographic scoring is scale-aware (P4 geohash default, raw haversine
  when the user gives an exact radius).
- Radius-bearing queries never reach `PLACE-ATTRIBUTE-QUERY` via the
  parser (deterministic override) or the executor (haversine guard).
- Tag filtering is key-presence first, value-match boost second. Entities
  missing the queried tag key are excluded before scoring.
- USLP signal boost is +0.5 and only applies to predicted link tails with
  normalized score >= 0.7.
