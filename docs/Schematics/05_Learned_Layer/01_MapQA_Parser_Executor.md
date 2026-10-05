# MapQA Parser + Executor: Natural Language → Geospatial Answer

> **Focus:** the MapQA pipeline end to end. The TF-IDF parser that turns a
> natural-language geospatial question into a template + concepts + roles,
> the executor that runs it against the data plane, and the HITL modal that
> lets a user approve or edit agent-proposed queries.
>
> **Key idea:** two planes, one seam. The parser is control-plane only
> (pure Python, no DB). The executor is data-plane only (PostGIS +
> pgvector + factor tables). The parsed DAG is the contract between them.

---

## 1. The Runtime Flow

```
User types: "Which bars are within 50m of Hollywood Blvd?"
  ▼  POST /api/nca/execute-query/  {query, country_code, snapshot_date}

  - QueryParserService.parse(question, country_code?, object_oracle?)
                                                  [control plane, pure Python]
      1. TF-IDF transform + MultinomialNB predict  → template + confidence
      2. OneVsRest LogReg predict                   → concept types present
      3. Span extraction                            → concept text values
      2c. Class-verified OBJECT recovery (object_oracle injected by the
          view; parser stays no-DB): ML+heuristic miss on #1 → geocode the
          pre-preposition span → POI WorldKG class → emit OBJECT with the
          original span at conf 0.6. Brand names parse without retraining.
      4. LogReg predict                             → role per concept
      5. DAG composition from template skeleton     → ordered node list
      6. G2 validation (role precedence)            → valid/violations

  - QueryExecutorService.execute()      [data plane, PostGIS + OsmEntity]
      1. Multi-entity extraction (for distance/direction/compare)
      2. Template-specific executor function:
         SUB_COND → search OsmEntity by amenity tag (3-tier fallback:
                    exact tag → WorldKG ontology class via DB mapping →
                    FastText semantic; proper-name OBJECTs add a 4th,
                    name-first tier: trigram on name_romanized merged into
                    the FastText pool)
         SUPPORT  → EntityGeocoder.geocode() → PostGIS name search
         COND     → filter by radius (PostGIS ST_DWithin, GiST index)
         MEASURE  → count / rank / distance / bearing
      3. Enrichment + re-rank (non-fatal):
         combined_score = diffusion + name_score + geo + uslp_boost
         (name_score = FastText cosine between the OBJECT span and the
         entity name; wired into #1 and #8)
      4. Answer synthesis (deterministic per template)

  ▼  Response JSON
  {
    "query": "...",
    "parsed": { "template", "concepts", "roles", "dag", "confidence", "validation" },
    "result":  { "answer", "results", "trace", "latency_ms" }
  }

  ▼  Frontend (SemanticSearchPanel.vue)
  - Template badge + confidence indicator (green/amber/red)
  - Concept chips (OBJECT: bar, AMOUNT: 50m, LOCATION: Hollywood Blvd)
  - Answer summary ("Found 12 entities within 50m.")
  - Collapsible execution trace (step-by-step operator log)
  - Results table + map markers (existing WorldKGMap.vue)
```

---

## 2. The Parser (Control Plane)

**Source:** `backend/semantic_search/services/query_parser_service.py`

`QueryParserService` is a singleton that loads pickled artifacts and exposes
`parse(question, country_code=None, object_oracle=None)` →
`{template, concepts, roles, dag, confidence, validation}`. It also exposes
`extract_all_entities(question)` for multi-entity templates.

**Stage 2c — class-verified OBJECT recovery (2026-09-28):** the ML
concept extractor misses open-vocabulary brand names and the heuristic
signal list doesn't cover them, so brand OBJECTs came back empty
("Juici Patties within 150km of KFC" → 0 results). When
`object_oracle` is injected (the view passes `_object_recovery_oracle`,
`worldkg_nca/views/search.py`) and ML + heuristic both miss OBJECT on #1,
the pre-preposition span is geocoded and its WorldKG class checked via
`is_poi_wkg_class` (POI accepted; Highway/Place/Waterway/Natural/
Boundary/Route/Railway/Landuse rejected). A POI-class span is emitted as
OBJECT with the original text at confidence 0.6. The parser itself stays
no-DB — the oracle is the caller's data-plane function. The discriminator
is pure and unit-tested (`test_mapqa_parser.py::TestIsPoiWkgClass`).

```python
parser = QueryParserService.get_instance()
parsed = parser.parse("Which bars are within 50m of Hollywood Blvd?")
# → {template: "FILTER-AGGREGATE-MEASURE (#1)", confidence: 1.0,
#    concepts: [{type: "OBJECT", text: "bar"}, ...],
#    roles: [{concept_type: "OBJECT", role: "SUB_COND"}, ...],
#    dag: [{role: "SUB_COND", ...}, ...],
#    validation: {valid: true, violations: []}}

entities = QueryParserService.extract_all_entities("How far is X from Y?")
# → ["X", "Y"]
```

**The 5 trained templates:**

| Template | Operator pattern |
|---|---|
| `FILTER-AGGREGATE-MEASURE (#1)` | place_search_spatial → geocode → ST_DWithin radius filter |
| `GEOCODE-BATCH-COMPARE (#4)` | place_search_spatial → geocode → Distance ordering (or compare_closer for 3+ entities) |
| `PLACE-ATTRIBUTE-QUERY (#8)` | place_search (name ILIKE) → place_details |
| `LOCATION-BEARING-CLASSIFY (#5)` | batch_geocode → bearing → bearing_to_direction |
| `OBJECT-FIELD-MEASURE (#2)` | batch_geocode → haversine |

### 2.1 Deterministic Template Override

Queries matching `within X(km|m) of` are forced to
`FILTER-AGGREGATE-MEASURE (#1)` regardless of TF-IDF output. This prevents
radius-bearing queries (e.g. "bar within 5km of a bus station") from being
misrouted to `PLACE-ATTRIBUTE-QUERY`, which uses heat kernel diffusion with
no geographic radius filter.

### 2.2 LLM Refine Path

When confidence falls below `MAPQA_LLM_FALLBACK_CONFIDENCE` (default 0.5),
`_llm_refine` re-classifies template/concepts via the platform LLM. The
refine path validates against `label_encoder.classes_`, so it only returns
templates the classifier knows.

---

## 3. The Executor (Data Plane)

**Source:** `backend/semantic_search/services/query_executor_service/`

`QueryExecutorService.execute(parsed, country, snapshot, question)` →
`{results, answer, trace, latency_ms}`. Five template-specific executors
plus:

- **3-tier amenity fallback**: exact tag → WorldKG ontology class (via the
  DB mapping `factor_amenity_class_mapping` / `FactorResolutionService.amenity_class`
  — rule 6.2, no hardcoded dicts) → FastText semantic (checks
  `factor_amenity_embedding` first). Each tier is traced with a `match_type`
  field.
- **PostGIS spatial search**: `ST_DWithin` (radius) and `Distance`
  (ordering) with the GiST index.
- **Radius guard in PLACE-ATTRIBUTE-QUERY**: heat kernel diffusion results
  are filtered by haversine distance when an AMOUNT concept is present.
  Prevents graph-connected but geographically distant entities from
  appearing (e.g. Pelican Bar at 19.5km returned in a 5km query).
- **USLP geographic scoring** (Mann et al. 2023 §3.3): `_geo_score_uslp()`
  encodes anchor and candidate coordinates to geohash at P4 precision
  (~39km cells), computes haversine between cluster centers, normalizes by
  d_max. FILTER-AGGREGATE-MEASURE with an explicit radius uses raw haversine
  with the user's radius as d_max. OBJECT-FIELD-MEASURE returns 0.0
  (distance IS the answer).
- **USLP signal boost**: entities that appear as predicted link tails
  (`SpatialTripletScore.predicted=True`) from the anchor get a +0.5 boost.
- **Re-rank (2026-09-28)**: `_enrich_with_geo_and_uslp()` re-ranks by
  `combined_score = diffusion_score + name_score + geo_score + uslp_boost`.
  `name_score` = FastText cosine between the OBJECT span and the entity
  `name` (proper-name OBJECTs only, decided by `_resolve_amenity_tag`
  returning None; exact brands ≈ 1.0, nameless 0.0). Wired into #1 and #8
  (previously only #8 — #1 returned the raw distance-sorted pool).
- **Name-first pool (2026-09-28)**: proper-name OBJECTs build the pool
  name-first. `_search_by_name()` (`spatial_search.py`) matches
  `name_romanized` with the index-assisted trigram operator (sim ≥ 0.4,
  fragment guard, `RomanizerRegistry` cross-script) plus an exact-name
  fallback; `_merge_pools()` dedupes by (osm_type, osm_id) against the
  FastText tier before the radius filter. The embedding pool admitted 1
  entity for "juici patties"; the name tier found 35 (trace step
  `place_search_name_pool`).
- **Geocoder**: strips leading articles ("a bus station" → "bus station"),
  treats `POINT(NaN NaN)` way geometries as no-coordinate, prefers entities
  with valid coordinates.
- **Spatial-filter trace**: when the anchor has no usable coordinates and a
  radius was requested, returns an empty list with a
  `spatial_filter_skipped` trace warning, never silent unfiltered results.

---

## 4. The DAG

The DAG is not a generic graph. It is a **list of nodes ordered by role
precedence**, composed from a pre-validated template skeleton. The template
provides the structure (which roles exist, in what order); the concepts
provide the content (what fills each role).

### 4.1 Role Precedence Invariant (G2)

```
SUB_COND (1) ≺ COND (2) ≺ SUPPORT (3) ≺ MEASURE (4)
```

Validated at runtime with numeric `ROLE_ORDER`, not lexicographic string
comparison. G1 (acyclicity), G3 (type compatibility), G4 (data
availability), G5 (connectivity) are satisfied by construction: the 5
template skeletons are pre-validated.

### 4.2 Example DAG: FILTER-AGGREGATE-MEASURE

```json
[
  {"role": "SUB_COND", "concept_type": "OBJECT",   "concept_text": "bar",          "operator": "place_search"},
  {"role": "COND",     "concept_type": "AMOUNT",   "concept_text": "50m",          "operator": "radius_filter"},
  {"role": "SUPPORT",  "concept_type": "LOCATION", "concept_text": "Hollywood Blvd","operator": "geocode"},
  {"role": "MEASURE",  "concept_type": "AMOUNT",   "concept_text": null,           "operator": "count"}
]
```

### 4.3 Open-Vocabulary OBJECT Extraction

`_extract_concept_span` for OBJECT tries the closed `amenity_vocab` first.
If no match, it returns the **raw phrase** before the first structural
preposition (open-vocabulary slot; the executor's 3-tier fallback resolves
it at execution time). Example: "italian food near a bus station" →
OBJECT "italian food", LOCATION "a bus station".

### 4.4 Kuhn's Template Hardening (2026-09-13)

Three fixes keep the template path correct when the input is dirty:

1. **Amenity correction layer** — `QueryCorrectionService` corrects
   misspelled amenity phrases ("resturant" → "restaurant") against the
   snapshot's *real* OSM tag vocabulary (`DISTINCT tags->>'amenity'`,
   cached per (snapshot, country), TTL 300s) using pg_trgm `similarity()`
   + fuzzystrmatch `levenshtein()` in one SQL pass (migration `0019`
   enables `fuzzystrmatch`). Tiers: exact → pg_trgm (sim ≥ 0.5) →
   fuzzystrmatch (≤ 3 edits), score floor 0.55; junk and open-vocabulary
   cuisine phrases ("italian food") fall through to the FastText tier
   unchanged. Wired into `_resolve_amenity_tag`, `_search_by_amenity`
   (step 2b, trace `match_type="fuzzy_correction"`), and
   `_amenity_candidate_osm_ids`.
2. **Question-word typo tolerance** — `_normalize_question_words` rewrites
   near-miss question words via *prefix-constrained* edit distance:
   "Whichs" → "Which" (the question word is a prefix of the token), while
   "Mill" → "Will" and "What" → "Who" are correctly left alone (not
   prefix-linked). Without it, a typo'd question word defeated the
   compare-closer patterns and leaked into entity extraction as a named
   entity. `_extract_entity_name` now anchors on the **earliest**
   preposition with a `:` span terminator.
3. **Geocoder fragment guards + NaN exclusion** — a short span ("Cliffs")
   no longer matches a much longer name ("Cliffs of Howth") at low trigram
   similarity (tier 1: sim < 0.6 AND name > 1.6x query; tier 3 icontains:
   same for queries < 8 chars); equal-length typos and partial names ≥ 8
   chars are preserved. `_search_by_amenity_spatial` excludes
   `POINT(NaN NaN)` geometries — `ST_Distance` on NaN returns 0, which
   ranked coordinate-less ways as "nearest (0m away)".

Result dicts carry `country_code`, letting the executor prune partition
queries to one country leaf (class-distribution context 12.4s → ~0.3s;
compare-closer candidate geocodes prune to the anchor's country with an
unpruned cross-country retry). Measured on the Moher Cottage compare
question: endpoint 40.4s → 9.0s.

---

## 5. HITL: Human-in-the-Loop Confirmation

**Source:** `frontend-v3/src/components/QueryConfirmationModal.vue`

The HITL modal renders editable concept slots for user approval of
agent-proposed queries:

```
Parsed Query (confidence: 0.87)
  Template: FILTER-AGGREGATE-MEASURE
  OBJECT:   [bars          ]  ← editable
  LOCATION: [Hollywood Blvd]  ← editable
  RADIUS:   [50m            ]  ← editable
  [Approve]  [Edit]  [Reject]
```

- **Approve**: send the parsed query to the executor as-is
- **Edit**: user modifies concept slots, then approves
- **Reject**: cancel, no execution

The HITL state is managed by `queryProposalStore.js` (Pinia store):
`idle` → `pending` → `approved`/`edited`/`rejected`. The modal is
store-driven and teleported to body (see rule 01 §1.12).

### 5.1 MCP Bridge (Dev-Only)

The HITL tools are exposed via the vue-mcp-server (dev-only MCP bridge at
`/__mcp`):

| MCP Tool | Purpose |
|---|---|
| `proposeQuery` | Submit a parsed query for user approval |
| `getApprovalState` | Check current approval state |
| `getQueryProposal` | Get the current query proposal |

This lets external agents (e.g. Devin, Goose) submit queries and check
approval status programmatically. HITL applies to agent-proposed queries;
the direct UI path executes without it.

---

## 6. Training

**Source:** `backend/semantic_search/management/commands/train_mapqa_parser.py`

```bash
python manage.py train_mapqa_parser
```

This trains:
1. TF-IDF vectorizer + MultinomialNB template classifier
2. OneVsRest Logistic Regression concept extractor
3. Multiclass Logistic Regression role assigner

Artifacts are saved to `backend/data/mapqa_parser/artifacts/`:
- `vectorizer.pkl`
- `template_classifier.pkl`
- `label_encoder.pkl`
- `concept_extractor.pkl`
- `role_assigner.pkl`

**Training data:**
- `docs/Schematics/MapQA-dataset-main/` (the upstream MapQA dataset with
  labeled templates and concepts), `california_full` (train) +
  `illinois_test` (zero-shot holdout).
- Hand-curated `natural_language_qa_pairs.csv` (Region=`augmented`).
- **Self-supervised rows (Region=`self_supervised`, implemented
  2026-08-25)**, PostGIS-verifiable Q&A pairs generated from `OsmEntity`
  ground truth in processed countries. See §9 below and
  `docs/plans/completed/MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md`.

**Train/test split** (see `Command._split_rows`):
- Train: `california_full` + `augmented` + `self_supervised`
- Zero-shot test: `illinois_test` (unchanged, the headline metric)
- Informational holdout: `self_supervised_test` (stratified 20% of
  generated rows; reported in `metrics.json`, never mixed into train)

The CSV loader tolerates both schemas. Legacy 7-column rows get defaults
(`Source` derived from `Region`: `mapqa-llm` / `hand-curated` /
`self_supervised`).

---

## 7. Testing

```bash
# Parser unit tests (22 tests, no DB needed)
docker compose exec backend python -m pytest tests/unit/test_mapqa_parser.py -v --reuse-db

# Executor integration tests (12 tests, needs DB)
docker compose exec backend python -m pytest tests/integration/test_mapqa_executor.py -v --reuse-db
```

34 tests covering:
- Template classification (5 templates)
- Confidence range [0, 1]
- Concept extraction (amenity, longest-match, radius, location)
- DAG validation
- Role precedence (G2)
- Multi-entity extraction
- Edge cases (empty/unknown/long questions)
- Endpoint 200/400
- Trace presence
- Unknown template
- Latency
- Answer correctness

---

## 8. Invariants

- The parser is control-plane only. It does NOT touch the data plane.
- The executor is data-plane only. It runs the parsed plan against PostGIS
  + pgvector.
- G2 role precedence: `SUB_COND (1) ≺ COND (2) ≺ SUPPORT (3) ≺ MEASURE (4)`
- Confidence range: [0, 1] for all parsed queries
- Amenity fallback: exact tag → ontology class → FastText (each tier traced);
  misspelled phrases are corrected via pg_trgm + fuzzystrmatch against the
  snapshot's real tag vocabulary before the FastText tier
- Question-word typos are normalized prefix-constrained: "Whichs" → "Which",
  never an entity word ("Mill" → "Will")
- Geocoder never resolves a short span to a much longer name at low
  similarity; NaN-geom entities never rank in spatial searches
- HITL applies to agent-proposed queries via MCP; the user approves, edits,
  or rejects before execution
- The MCP bridge is dev-only (`/__mcp` endpoint, not exposed in production)

---

## 9. Training Data Distribution

The parser is trained on the MapQA LLM dataset (California + Illinois
splits), a hand-curated augmentation CSV, **and self-supervised rows**
generated from the platform's own `OsmEntity` ground truth (implemented
2026-08-25, `docs/plans/completed/MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md`
Part 1). The raw MapQA dataset covers 5 of the 9 templates. The 4
graph-powered templates (`SPECTRAL-ANALYSIS`, `TEMPORAL-DRIFT`,
`COMMUNITY-DETECT`, `EVENT-DIFFUSION`) have no training data in the source
dataset and are currently exposed only via dedicated API endpoints that
bypass the classifier (Part 2 of the expansion plan will add them on top of
this infrastructure).

Distribution after the 2026-08-25 self-supervised expansion
(`natural_language_qa_pairs.csv`, 41 hand-curated + 7,424 generated):

| Template | Source dataset (train+test) | Self-supervised | Total |
|---|---|---|---|
| `GEOCODE-BATCH-COMPARE` (#4) | 1,198 | 1,500 | 2,698 |
| `FILTER-AGGREGATE-MEASURE` (#1) | 756 | 1,511 | 2,267 |
| `PLACE-ATTRIBUTE-QUERY` (#8) | 600 | 1,454 | 2,054 |
| `LOCATION-BEARING-CLASSIFY` (#5) | 300 | 1,500 | 1,800 |
| `OBJECT-FIELD-MEASURE` (#2) | 300 | 1,500 | 1,800 |
| `SPECTRAL-ANALYSIS` (#11) | 0 | 0 | API-only |
| `TEMPORAL-DRIFT` (#12) | 0 | 0 | API-only |
| `COMMUNITY-DETECT` (#13) | 0 | 0 | API-only |
| `EVENT-DIFFUSION` (#14) | 0 | 0 | API-only |

Every generated answer is computed exactly from PostGIS at generation time
(`ST_DWithin` counts, haversine, nearest-entity ordering, bearing→cardinal)
and is recomputable at test time, stronger than the heuristic annotation in
the original dataset. Generation is deterministic and idempotent:

```bash
python manage.py generate_mapqa_training_data --all-processed [--retrain]
```

Run `python manage.py mapqa_dataset_metrics` to regenerate the distribution
report. Current headline metrics (2026-08-25): Illinois zero-shot acc
**0.9958**, self-supervised holdout acc 0.9818, F1-macro 0.9914.
