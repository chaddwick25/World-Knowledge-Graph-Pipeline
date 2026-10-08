# Two-Axis vs Single-Axis Embedding: The Decision and the Naming

> **Status: SUPERSEDED (2026-09-10).** The two-axis Step-1 encoding path
> (`DualEncodingWriter` + `_build_dual_writer` + `_run_parallel_dual` +
> `_PARALLEL_DUAL_ENABLED`) was **removed** as dead code — see
> `docs/issues/TICKET_REMOVE_DUAL_ENCODER.md`. It never ran in production
> (zero country-level `wdw.pickle` on disk, verified 2026-08-31).
>
> What remains:
> - **Step 1** is FastText-only (GV-Tags 300D) — `DBOnlyWriter`
>   (single-threaded) or `BatchCollector` fan-out (parallel); both paths in
>   `EmbeddingService.run()`.
> - **Step 5** trains GV-NLE 100D (weighted DeepWalk on the k-NN graph) and
>   writes `wdw.pickle` via `GeoVectorsEncoderService.generate_subgraph_pickle()`.
> - The **inductive bridge** (`InductiveSpatialService`, BallTree + IDW)
>   computes provisional GV-NLE at query time.
>
> Sections 4 (`DualEncodingWriter`) and 5.2 (`_run_parallel_dual`) describe
> removed code and are kept for history only. The name analysis below
> ("dual" is a misnomer) remains useful.

> **Focus:** how `EmbeddingService` branches between single-axis encoding
> (FastText-only, `DBOnlyWriter`) and two-axis encoding (FastText + NLE,
> `DualEncodingWriter`), and how the `_PARALLEL_DUAL_ENABLED` phase gate
> controls the parallel path.
>
> **Key idea:** the pipeline produces two orthogonal embeddings per OSM
> entity, 300D GV-Tags (semantic, "what something is") and 100D GV-NLE
> (spatial, "where something is"). Whether both are computed during Step 1
> depends on whether a pre-trained NLE pickle exists for the country.
>
> **Terminology note (2026-08-31):** "dual encoder" is a misnomer. The
> GeoVectors paper (Tempelmeier et al., ISWC 2021) defines **two embedding
> models**, GV-Tags via fastText, GV-NLE via weighted DeepWalk, each with
> its own training and encoding phases; there is no shared "dual"
> architecture. At inference neither axis is a learned encode: GV-Tags is
> word-vector lookup + mean (aggregation), GV-NLE is PostGIS 50-NN + IDW
> blend (interpolation). "Dual" in the code means only "both columns
> populated in one Step-1 pass" (a provisioning property). Prefer
> **two-axis** terminology; see the `geovectors_encoder` notebook §2 for the
> full naming analysis.
>
> **Likely origin of the name:** the one place the two axes genuinely
> "become one" is the fused 400D `static_embedding` (concat of GV-Tags |
> GV-NLE). "Dual" probably referred to that fusion. But the name was applied
> to the wrong object: `DualEncodingWriter` never fuses anything; it merely
> runs two independent encoders in one pass. The actual fusion lives in
> `compute_static_embeddings` (not materialized, see §7).

---

## 1. The Two-Axis Architecture

The GeoVectors paper (Tempelmeier et al., ISWC 2021) defines two independent
embedding models. The pipeline implements both, plus a fused 400D
`static_embedding` for unified ANN search:

| | Axis 1: semantic (300D GV-Tags) | Axis 2: spatial (100D GV-NLE) |
|---|---|---|
| Meaning | "What something is" | "Where something is" |
| Source | OSM tags → FastText | k-NN graph → DeepWalk |
| Encoder | `FastTextModel.encode_instance` | `NLEModel.encode_coords` |
| Storage | `gv_tags_embedding` (VECTOR 300) | `gv_nle_embedding` (VECTOR 100) |
| Index | (fused into static_embedding) | (fused into static_embedding) |
| Cost | ~1s per 20K batch (C++ lookup) | 10-50x more (PostGIS KNN + Node2Vec) |
| Requires pickle | NO | YES (pre-trained NLE) |

Fused: `static_embedding` (VECTOR 400) = concatenate(GV-Tags(300),
GV-NLE(100)). HNSW index planned, **not built** (0 rows populated, verified
2026-09-09, see §7).

**Source:** <ref_file file="backend/core/services/snapshot/embedding_service.py" />

---

## 2. The Decision Branch

`EmbeddingService.run()` was the single entry point for Step 1 encoding and
branched on two envelope fields: `cfg.has_pretrained_nle` and
`cfg.pickle_path`. **That branch no longer exists** — the two-axis path was
removed 2026-09-10 (see the superseded banner above).

Current Step 1 flow:

```
EmbeddingService.run(cfg)
  workers = _parallel_upsert_workers()
  if workers > 1:
    _run_parallel(cfg, pbf_path, workers, queue_depth)   # BatchCollector fan-out, FastText-only
  else:
    writer = DBOnlyWriter(FastTextModel(), tags_storage) # legacy single-threaded, FastText-only
```

No NLE encoding happens in Step 1. `cfg.has_pretrained_nle` is still carried
on the envelope and reported in the result dict (`has_nle`), but it no
longer routes any code path.

### 2.1 What Controlled the Branch (historical)

| Field | Source | Meaning |
|---|---|---|
| `cfg.has_pretrained_nle` | `CountryEnvelope.from_db()` → `CountryPipelineProfile` | Whether the country has a pre-trained NLE model available |
| `cfg.pickle_path` | `CountryEnvelope.from_db()` → resolved filesystem path | Path to the `wdw.pickle` (DeepWalk model) for the NLE encoder |
| `workers` | `PARALLEL_UPSERT_WORKERS` env var (code default 1) | Encoding thread count; `1` = legacy single-threaded, `>1` = parallel path |
| ~~`_PARALLEL_DUAL_ENABLED`~~ | ~~Module constant in `embedding_service.py`~~ | ~~Removed 2026-09-10~~ |

### 2.2 Why the Two-Axis Path Never Ran (verified 2026-08-31 against live DB + disk)

- **All 197 countries** have `has_embeddings = t` in
  `country_pipeline_profiles`, so gate 1 (`has_pretrained_nle`) passed
  everywhere.
- **Zero country-level `wdw.pickle` files** existed on the worker filesystem
  (checked `find /app/data -name wdw.pickle` excluding the `pickles/`
  subgraph paths), so `cfg.pickle_path = None` everywhere, gate 2 failed, the
  two-axis path never ran.
- Ireland has **104 subgraph pickles** at `pickles/{subgraph}/wdw.pickle`,
  but the envelope resolves the singular `pickle/` dir (`get_pickle_dir` →
  `pickle/wdw.pickle`), so even Ireland never activates the country-level
  two-axis path. A path-convention mismatch (`pickles/` vs `pickle/`).
- `parallel_upsert.workers=1` (in `pipeline/hyperparams.yaml`); the I/O-bound ceiling
  from schematic 02 is the live configuration.

---

## 3. DBOnlyWriter: Single Encoding (FastText-only)

**Source:** <ref_file file="backend/geovectors_encoder/services/geovectors_service.py" /> (`DBOnlyWriter` moved to `geovectors_encoder/services/snapshot_processors.py` 2026-09-25)

`DBOnlyWriter` is the single-encoder writer. It holds one encoder (FastText
or NLE) and one storage service. For each record:

1. `encoder.encode_instance(record)` → produces a vector
2. `storage.upsert_batch([encoded_record])` → SQL COPY into `OsmEntity`

When used for FastText-only encoding (the production default):
- `encoder` = `FastTextModel`, calls `get_word_vector()` per tag key and per
  value token, **unweighted arithmetic mean** (centroid) of all token
  vectors → 300D vector. C++ lookup, releases GIL, ~1s per 20K batch.
- `storage` = `VectorStorageService(model_type="tags")`, upserts into the
  `gv_tags_embedding` column.
- `gv_nle_embedding` stays NULL (filled later by Step 5 DeepWalk training,
  or by the inductive path, see
  [Inductive BallTree IDW](../05_Learned_Layer/03_Inductive_BallTree_IDW.md)).

> **In-tree extensions (not in paper or reference):**
> 1. **L2 normalization**: the reference (`GeoVectors-master/FasttextModel.py`)
>    returns the raw centroid vector. The in-tree `FastTextModel.encode_tags()`
>    divides by `np.linalg.norm(enc)` after the mean. This ensures manifold
>    consistency for cosine similarity with the search API (cosine = dot
>    product on unit-norm vectors). GV-Tags embeddings must have norm ≈ 1.0
>    **because of this step**, not because the paper requires it.
> 2. **Multi-word value tokenization**: the reference skips tag values that
>    contain spaces (e.g. `"New York"` → skipped). The in-tree code tokenizes
>    all words (`["New", "York"]` → two FastText lookups). This gives richer
>    coverage for multi-word amenity names and descriptions.

```python
# embedding_service.py (legacy single-threaded path, FastText-only)
writer = DBOnlyWriter(ft_model, tags_storage)
```

---

## 4. DualEncodingWriter: Dual Encoding (FastText + NLE) — REMOVED 2026-09-10

> This class was deleted with the two-axis Step-1 path (see the superseded
> banner above). Kept here for history.

**Source:** <ref_file file="backend/geovectors_encoder/services/geovectors_service.py" /> (search for `class DBOnlyWriter` — `DualEncodingWriter` no longer exists)

`DualEncodingWriter` held **two** encoders and **two** storage services.
For each record, it encoded both axes and upserted both:

1. `tag_encoder.encode_instance(record)` → 300D GV-Tags
2. `nle_encoder.encode_coords(lat, lon)` → 100D GV-NLE (via PostGIS KNN query
   against already-embedded entities + IDW-weighted mean)
3. `tag_storage.upsert_batch(...)` + `nle_storage.upsert_batch(...)` → both
   columns populated in one pass

The `NLEModel` is loaded from the pickle path:
- `NLEModel(pickle_dir, njobs=1, db=DjangoPostgresDB())`
- `nle_model.load_indexes()` loads the `wdw.pickle`, a `WDWStore`: dict of
  `{osm_type}_{osm_id}` → 100D reference embeddings + `n_components`
  metadata. **Not** a BallTree; the BallTree is rebuilt in-memory when
  needed (see
  [03_BallTree_Pickle_Generation.md](03_BallTree_Pickle_Generation.md)).
- The NLE encoder issues a PostGIS 50-NN geographic query per entity
  (`st_distance` + `<->` operator), then applies IDW damped weights to
  compute a proximity-weighted mean of the 50 nearest GV-NLE vectors.

This is 10-50x more expensive than FastText-only encoding, which is why the
parallel dual path exists (see
[Streaming Batched Encoding](02_Streaming_Batched_Encoding.md)).

---

## 5. The Parallel Paths

### 5.1 `_run_parallel`: FastText-only, Parallelized (the only parallel path)

- Main thread streams the PBF once via `read_from_snapshot` with a
  `BatchCollector` (drop-in writer replacement).
- N encoding threads (enabled via `PARALLEL_UPSERT_WORKERS > 1`, code
  default 1) pull 20K batches from a bounded queue, encode with FastText
  (GIL-free C++ lookup), push encoded batches onto a second bounded queue.
- **One** upsert thread serializes all DB writes through a single
  `VectorStorageService` to avoid lock contention on the leaf partition's
  unique index.
- See [Streaming Batched Encoding](02_Streaming_Batched_Encoding.md) for the
  full `BatchCollector` architecture.

### 5.2 `_run_parallel_dual`: FastText + NLE, Parallelized — REMOVED 2026-09-10

> Removed with the two-axis path. It had the same producer/consumer topology
> as `_run_parallel`, shared one `NLEModel` across encoding threads, and the
> single upsert thread flushed both `VectorStorageService` instances via a
> `CombinedStorage` wrapper.

---

## 6. Why Two-Axis Encoding Existed (and Why It Was Never the Default)

The GeoVectors paper (Section 3.2) defines both embeddings. The original
GeoVectors reference implementation (`papers/GeoVectors-master/`) computed
both in a single pass. The pipeline preserved this capability via
`DualEncodingWriter` but defaulted to FastText-only because:

1. **GV-NLE is expensive.** The NLE encoder issues a PostGIS 50-NN query per
   entity (10-50x the cost of FastText). For countries without a pre-trained
   NLE pickle, this would require building the k-NN graph first.
2. **Step 5 trains GV-NLE separately.** DeepWalk training on the k-NN graph
   (Step 5) produces the authoritative GV-NLE embeddings. The two-axis path
   was for countries that already have a pre-trained pickle and wanted
   provisional NLE during Step 1.
3. **The inductive path covers new entities.** For entities without a
   pre-trained NLE, `InductiveSpatialService` computes a provisional GV-NLE
   via BallTree + IDW at query time (see
   [Inductive BallTree IDW](../05_Learned_Layer/03_Inductive_BallTree_IDW.md)).

The decision is now **resolved**: Step 1 is FastText-only, period; GV-NLE
comes exclusively from Step 5 training and the inductive bridge.

---

## 7. The 400D Fused static_embedding

After both axes are populated, `compute_static_embeddings` concatenates
them:

```
static_embedding (400D) = concatenate(gv_tags_embedding(300D), gv_nle_embedding(100D))
```

This is the pipeline's design choice, not from the paper. The paper uses the
two embeddings separately (GV-Tags for type assertion, GV-NLE for link
prediction). The fusion exists for unified ANN search via a single HNSW
index on `static_embedding`. Only rows with **both** GV-Tags + GV-NLE have
`static_embedding` populated.

**Build:** `python manage.py compute_static_embeddings` (after Step 5)
**Index:** `python manage.py create_static_embedding_hnsw_index`

> **Verified 2026-08-31, re-verified 2026-09-09:** `static_embedding` is
> NULL for **all** rows in the vectors DB (0 of 46,030,536; both-axes
> coverage 2,160,420 = 4.7%). `compute_static_embeddings` has not been run
> and no `static_embedding` HNSW index exists. The only HNSW indexes present
> are on the per-snapshot materialized views (not read by runtime queries).
> The 400D fusion is a *design*, not a live artifact; the HNSW-on-400D search
> path serves nothing until it is built (plan:
> `docs/plans/FUSED_400D_ANN_DEDUP_PLAN.md`).

---

## 8. Invariants

- Aggregation: GV-Tags embeddings are the **unweighted arithmetic mean**
  (centroid) of per-token FastText vectors, not a weighted average. Both the
  paper (Section 3.1) and the reference implementation use
  `np.mean(vectors, axis=0)` with equal weight per token.
- L2 normalization: GV-Tags embeddings must have norm ≈ 1.0 **due to the
  in-tree `FastTextModel.encode_tags()` L2-normalization step**, an
  extension not present in the paper or `GeoVectors-master`. It ensures
  cosine similarity equals dot product, which is required by the pgvector
  HNSW index (`vector_cosine_ops`).
- Multi-word tokenization: the in-tree encoder tokenizes all value words
  (e.g. `"New York"` → `["New", "York"]`); the reference skips multi-word
  values. A deliberate improvement for amenity-name coverage.
- Cosine distance range: [0, 2] with similarity = 1 - distance
- GV-NLE coverage: entities should have both semantic and spatial embeddings
- Same-type similarity: entities with same tags should have high similarity
- `static_embedding` is only populated when both GV-Tags + GV-NLE are present
- Step 1 is FastText-only: the `_PARALLEL_DUAL_ENABLED` gate and the parity
  precondition (`PARALLEL_UPSERT_APPROACH_B_PLAN.md` §5.2) are moot — the
  gate, `DualEncodingWriter`, and `_run_parallel_dual` were removed
  2026-09-10.
