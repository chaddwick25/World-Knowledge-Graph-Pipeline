# IGEA: Iterative Geographic Entity Alignment (Step 3)

> **Focus:** how IGEA iteratively aligns OSM entities with Wikidata knowledge
> graph entries using semantic + geo-spatial features, a per-country
> cross-attention BiLSTM, and Neural Class Alignment (NCA) for tag→class
> mapping.
>
> **Key idea:** IGEA is an **iterative bootstrapping** loop. Start with a
> seed set of high-confidence alignments (OSM entities that already have a
> `wikidata=` tag), train a cross-attention model on those seeds, use the
> model to find new alignments, add the new alignments to the seed set, and
> repeat. Each iteration expands the seed set and improves the model. NCA
> runs inside the loop to learn tag→class mappings from the growing seed set.

---

## 1. Where IGEA Sits in the Pipeline

**Step 1: Embed OSM entities** (FastText + optional NLE) → `OsmEntity` rows
with `gv_tags_embedding`, `tags`, `geom`, `wkg_class`.

**Step 2: Harvest Wikidata candidates** (SPARQL) →
`PrecomputedLinkCandidate` rows with `wikidata_uri`, `label`, `wkg_class`.

**Step 3: IGEA — iterative geographic entity alignment** (this doc):
- Input: `OsmEntity` (vectors DB) + `PrecomputedLinkCandidate` (vectors DB)
- Output: `OsmEntity.wikidata_uri` populated for aligned entities
- Output: `EntityAlignment` rows (audit trail with confidence + iteration)

**Step 4: USLP — unsupervised spatial link prediction** → uses aligned
entities to predict spatial relationships.

**Source:** <ref_file file="backend/igea/services/igea_pipeline_service.py" />

---

## 2. The Iterative Bootstrapping Loop

1. **Harvest candidates.** `WikidataCandidateService.harvest_by_country(
   iso, limit=50K)` → `PrecomputedLinkCandidate` rows (`wikidata_uri`,
   `label`, `class`).
2. **Backfill embeddings.** For candidates missing `gv_tags_embedding`:
   `FastText.encode(label + class)` → 300D vector.
3. **Seed alignment.** `OsmEntity.objects.exclude(tags__wikidata__isnull=
   True)` → `seed_map: {osm_id → wikidata_uri}` (entities that already have
   a `wikidata=` tag in OSM).
4. **Iterative loop (max 3 iterations):**
   a. **Candidate pairs.** Pair OSM entities with Wikidata candidates.
      Filter by: `max_distance` (2500m), polygon WKT.
   b. **Train cross-attention (BiLSTM).** Train on (matched, unmatched)
      pairs from the seed set. 10 epochs, batch 32, Adam, BCE loss. Falls
      back to cosine if training fails.
   c. **Score pairs.** `score = cross_attention_model.predict(osm,
      wikidata)`. Fallback: `cosine(gv_tags_embedding, candidate_emb)`.
   d. **Accept.** `if score >= threshold (0.6)` →
      `accepted.append((osm_id, wikidata_uri))`.
   e. **Write to DB.** `OsmEntity.wikidata_uri = wikidata_uri`;
      `EntityAlignment(alignment_method, confidence, iter)`.
   f. **Expand seed set.** `seed_map.update(accepted)`.
   g. **NCA (neural class alignment).** Learn tag→class mappings from the
      expanded seed set; apply mappings to unaligned entities; updates
      `wkg_class` on `OsmEntity` rows.
5. **Return stats.** `{total_accepted, iterations_run, per_iteration: [...]}`.

**Source:** <ref_file file="backend/igea/services/iterative_alignment_service.py" />

---

## 3. The Three Feature Spaces

IGEA uses three feature spaces to score OSM↔Wikidata pairs.

### 3.1 Semantic (`gv_tags_embedding`, 300D)

- **OSM side**: `OsmEntity.gv_tags_embedding`, FastText weighted average of
  tag key/value embeddings (from Step 1).
- **Wikidata side**: FastText encoding of the Wikidata label + class
  (backfilled in step 2 of the IGEA flow).
- **Comparison**: cosine similarity or cross-attention probability.

### 3.2 Geo-Spatial (Haversine Distance)

- **Max distance**: 2500 meters (hardcoded default).
- **Filter**: pairs beyond `max_distance_m` are not candidate pairs.
- **Polygon filter**: only entities within the country's polygon WKT are
  considered (prevents cross-border false positives).

### 3.3 Class (`wkg_class`)

- **OSM side**: `OsmEntity.wkg_class`, from Step 1 SQL enrichment or NCA.
- **Wikidata side**: `PrecomputedLinkCandidate.wkg_class`, from Step 2
  SPARQL harvest.
- **Purpose**: type compatibility check. A "restaurant" OSM entity should
  align with a "restaurant" Wikidata entry, not a "country".

---

## 4. Cross-Attention BiLSTM

**Source:** `backend/igea/services/iterative_alignment_service.py` (`_train_cross_attention`)

The cross-attention model is a **BiLSTM** (Bidirectional Long Short-Term
Memory) network trained per-country on matched/unmatched pairs.

### 4.1 Architecture

1. Input: `(osm_embedding(300D), wikidata_embedding(300D))`
2. **BiLSTM layer** (bidirectional, hidden_size=128), captures sequential
   dependencies in the embedding dimensions.
3. **Cross-attention layer**, learns which dimensions of the OSM embedding
   attend to which dimensions of the Wikidata embedding.
4. **Dense layer** (sigmoid activation) → probability [0, 1].
5. Output: alignment probability.

### 4.2 Training

- **Optimizer**: Adam
- **Loss**: Binary Cross-Entropy (BCE)
- **Epochs**: 10
- **Batch size**: 32
- **Training data**: matched pairs (from seed set) + unmatched pairs
  (random OSM↔Wikidata combinations)
- **Per-country**: the model is trained fresh for each country run. It
  learns country-specific alignment patterns.

### 4.3 Fallback

If cross-attention training fails (e.g. not enough seed pairs), the scorer
falls back to **cosine similarity** between `gv_tags_embedding` and the
candidate embedding. Less accurate but always available.

---

## 5. NCA: Neural Class Alignment

**Source:** `backend/igea/services/iterative_alignment_service.py` (`_run_nca`)

NCA runs **inside** the iterative loop (step 4g). It learns tag→class
mappings from the growing seed set and applies them to unaligned entities.

### 5.1 Lifecycle

1. `build_vocabularies(linked_entities)` → extract tag keys/values and
   Wikidata classes from linked entities
2. `prepare_training_data(linked_entities)` → create multi-hot vectors
   (tags) and labels (classes)
3. `train()` → fit the neural model
4. `extract_mappings(threshold)` → probe the model to get (tag, class,
   confidence) triples
5. `apply_mappings(osm_entities)` → assign `wkg_class` to entities using
   learned mappings

### 5.2 The wikidata_uri Attachment Fix

**Source:** `iterative_alignment_service.py` lines 257-265

NCA needs `wikidata_uri` on linked entities to build class vocabularies.
Without it, NCA fails with "Vocabularies not built". The fix attaches the
URI from the seed map before passing to NCA:

```python
linked: List[Dict] = []
for e in osm_entities:
    osm_id = e.get('osm_id')
    if osm_id in seed_map:
        ent = dict(e)
        ent['wikidata_uri'] = seed_map[osm_id]  # ← the fix
        linked.append(ent)
```

### 5.3 Minimum Entities

NCA requires at least 10 linked entities to train. Below that, the
vocabularies are too sparse and NCA is skipped.

---

## 6. The EntityAlignment Audit Model

**Source:** <ref_file file="backend/igea/models.py" />

```python
class EntityAlignment(models.Model):
    id = UUIDField(primary_key=True)
    osm_type = CharField(max_length=10, indexed)
    osm_id = BigIntegerField(indexed)
    wikidata_uri = CharField(max_length=200, indexed)
    wikidata_label = CharField(max_length=500, null=True)
    alignment_method = CharField(choices=[
        ('SEED', 'Seed'),           # from OSM wikidata= tag
        ('COSINE', 'Cosine'),       # fallback cosine similarity
        ('CROSS_ATTN', 'CrossAttn') # cross-attention BiLSTM
    ])
    confidence = FloatField()       # 0.0-1.0
    iteration = IntegerField()      # which IGEA iteration found this
    distance_meters = FloatField()  # Haversine distance
    created_at, updated_at = DateTimeFields()
    unique_together = [['osm_type', 'osm_id', 'wikidata_uri']]
```

Every alignment is recorded with:
- **Method**: how the alignment was found (seed / cosine / cross-attention)
- **Confidence**: the score from the model
- **Iteration**: which bootstrapping iteration found it
- **Distance**: geographic distance between the OSM entity and Wikidata
  entry

This provides a full audit trail. Any alignment can be traced back to its
method, confidence, and iteration.

---

## 7. Configuration

### 7.1 Hardcoded Defaults (NOT in hyperparams.yaml)

IGEA does **not** use `hyperparams.yaml`. Its defaults are hardcoded in
`igea_pipeline_service.py`:

| Parameter | Default | Purpose |
|---|---|---|
| `max_iterations` | 3 | Maximum bootstrapping iterations |
| `threshold` | 0.6 | Acceptance threshold for alignment score |
| `max_distance_m` | 2500.0 | Max Haversine distance for candidate pairs |
| `enable_nca` | True | Run NCA inside the loop |
| `enable_cross_attention_training` | True | Train BiLSTM per country |

### 7.2 Envelope Fields

| Field | Source | Purpose |
|---|---|---|
| `env.iso` | `CountryEnvelope.from_db()` | Country code for candidate harvest |
| `env.poly_path` | `CountryEnvelope.from_db()` | Polygon file for spatial filter |
| `env.snapshot_pbf_path` | `CountryEnvelope.from_db()` | Fallback for poly_path |
| `env.snapshot_date` | `CountryEnvelope.from_db()` | snapshot_id for entity queries |

---

## 8. Data Flow

**Inputs:**
- `OsmEntity` (vectors DB): `gv_tags_embedding` (300D, from Step 1), `tags`
  (JSONB, including `wikidata=` tag for seeds), `geom` (PostGIS point),
  `wkg_class` (from Step 1 SQL enrichment), `snapshot_id`, `country_code`
  (partition pruning)
- `PrecomputedLinkCandidate` (vectors DB, from Step 2): `wikidata_uri`,
  `wikidata_label`, `wkg_class` (from Step 2 SPARQL + Redis map),
  `gv_tags_embedding` (backfilled by IGEA if missing)

**Outputs:**
- `OsmEntity` (updated): `wikidata_uri` = aligned Wikidata URI,
  `wkg_enriched_at` = timestamp
- `EntityAlignment` (new rows, audit trail): `osm_type`, `osm_id`,
  `wikidata_uri`, `wikidata_label`, `alignment_method`
  (SEED/COSINE/CROSS_ATTN), `confidence` (0.0-1.0), `iteration` (1-3),
  `distance_meters`

---

## 9. USLP Independence

USLP (Step 4) runs **independently**. It is no longer gated on IGEA
acceptance count. The `total_accepted == 0` checks were removed from
`step_4_predict_spatial_links` and `_run_subgraph_uslp`. This means:

- IGEA can produce 0 alignments (e.g. no seed entities) and USLP still runs.
- USLP uses the `wikidata_uri` field on `OsmEntity` if present, but does not
  require it.
- The two stages are decoupled. IGEA failure does not block USLP.

---

## 10. Invariants

- IGEA is an iterative bootstrapping loop: seed → train → score → accept →
  expand seed → repeat (max 3 iterations).
- The cross-attention BiLSTM is trained per-country, not globally.
- NCA runs inside the loop to learn tag→class mappings from the growing
  seed set.
- The `wikidata_uri` attachment fix is required for NCA vocabulary building.
- Every alignment is recorded in `EntityAlignment` with method, confidence,
  and iteration.
- IGEA does NOT use `hyperparams.yaml`. Defaults are hardcoded.
- USLP is NOT gated on IGEA. The two stages are decoupled.
- The acceptance threshold is 0.6 (lower than USLP's 0.7).
