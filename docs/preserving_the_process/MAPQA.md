# Preserving the MapQA Model Architecture

How the natural-language geospatial question answering methodology from the [MapQA: Open-domain Geospatial Question Answering](https://papers/WernerKuhn/MapQA:%20Open-domain%20Geospatial%20Question%20Answering.pdf) research paper (Kuhn et al.) was integrated into the Django pipeline.

---

## 1. Template Classification (TF-IDF + Naive Bayes)

### The Original Implementation
The MapQA paper frames geospatial question answering as a template-classification problem. Natural-language questions are classified into a library of geospatial templates, each defining a structured query skeleton with concept slots and role assignments. The paper uses TF-IDF vectorization over question text and a Naive Bayes classifier for template selection.

### The Updated Implementation
The pipeline preserves the paper's classification approach using scikit-learn, trained on a self-supervised question corpus generated from real OSM data.

* **Mechanism:** `QueryParserService` in `backend/semantic_search/services/query_parser_service.py` loads a pretrained `TfidfVectorizer` (ngram_range=(1,2), min_df=2, sublinear_tf=True) and `MultinomialNB` (alpha=0.1) classifier at startup.
* **Transformation:** A user question is vectorized, classified into one of 5 trained templates, and the max posterior probability becomes the confidence score. Below `MAPQA_LLM_FALLBACK_CONFIDENCE` (default 0.5), the parser falls back to an LLM for template assignment.
* **Training:** `backend/semantic_search/management/commands/train_mapqa_parser.py` trains on a mix of California, augmented, and self-supervised questions, with an Illinois zero-shot test set and a self-supervised holdout.

**The 5 Trained Templates:**

| ID | Template Name | Description |
|----|---------------|-------------|
| #1 | `FILTER-AGGREGATE-MEASURE` | Count amenities within a radius of a location |
| #2 | `OBJECT-FIELD-MEASURE` | Distance between two geocoded entities |
| #4 | `GEOCODE-BATCH-COMPARE` | Nearest amenity or compare-closer between multiple locations |
| #5 | `LOCATION-BEARING-CLASSIFY` | Bearing or directional cone between two locations |
| #8 | `PLACE-ATTRIBUTE-QUERY` | Adjacent amenity or attribute lookup (heat kernel when factor tables available) |

**Paper vs Code:** The paper describes a 10-template library. The implementation trains 5 templates with full coverage; the remaining 5 have 0% coverage and are not trained. An additional 4 spectral/graph templates (#11 SPECTRAL-ANALYSIS, #12 TEMPORAL-DRIFT, #13 COMMUNITY-DETECT, #14 EVENT-DIFFUSION) are wired in the executor but require factor-table data that the parser does not currently emit.

**Code Reference:** `backend/semantic_search/services/query_parser_service.py`
```python
label_idx = self.classifier.predict(X)[0]
template = self.label_encoder.inverse_transform([label_idx])[0]
confidence = float(max(self.classifier.predict_proba(X)[0]))
```

---

## 2. Concept Extraction (OneVsRest Logistic Regression)

### The Original Implementation
The MapQA paper uses Kuhn's core concepts of spatial information as the feature space for concept extraction. Each question is decomposed into typed concept slots (location, object, field, network, etc.) that fill the template skeleton.

### The Updated Implementation
The pipeline implements concept extraction as a multi-label classification problem using `OneVsRestClassifier(LogisticRegression)`, combining ML predictions with heuristic regex extraction.

* **Mechanism:** `_extract_concepts()` in `query_parser_service.py` predicts a 7-label binary vector over the concept vocabulary, then merges with heuristic extraction (regex for radii, amounts, location names).
* **Concept Vocabulary:** `CONCEPT_TYPES = ["LOCATION", "OBJECT", "FIELD", "EVENT", "NETWORK", "AMOUNT", "PROPORTION"]`
* **Transformation:** The ML model predicts which concept types are present; the heuristic layer extracts concrete values (e.g., "50m" → AMOUNT=50, "Hollywood Blvd" → LOCATION). The two are merged with `int(a or b)` per label.
* **Training:** `OneVsRestClassifier(LogisticRegression(class_weight="balanced", solver="liblinear", penalty="l2", C=1.0))` in `train_mapqa_parser.py`.

**Paper vs Code:** The implementation's concept vocabulary is derived from the MapQA/Spatial-Agent template requirements, not a literal Kuhn ontology. The active labels in the 5 trained templates are `LOCATION`, `OBJECT`, `AMOUNT`, `FIELD`. `NETWORK`, `EVENT`, `PROPORTION` are in the vocabulary but not active in the 5-template DAGs. There is no `neighborhood` concept. See `KUHN_CORE_CONCEPTS.md` for the full mapping analysis.

**Class-verified OBJECT recovery (Stage 2c, 2026-09-28):** The ML concept
extractor misses open-vocabulary brand names ("kfc", "juici patties") and
the heuristic signal-word list doesn't cover them, so the OBJECT slot came
back empty and the executor had no search target ("Juici Patties within
150km of KFC" returned 0 results). `parse()` accepts an injected
`object_oracle` (data plane; the parser itself stays no-DB). When the ML +
heuristic both miss OBJECT on FILTER-AGGREGATE-MEASURE, the deterministic
pre-preposition span is geocoded and its WorldKG class checked
(`is_poi_wkg_class` — POI classes accepted, Highway/Place/Waterway/etc.
rejected): a POI-class span is emitted as OBJECT with the original text at
confidence 0.6, no retraining. Roles, DAG, and G2 flow through unchanged.
Wired from `execute_query` / `execute_query_stream` via
`_object_recovery_oracle` (`worldkg_nca/views/search.py`).

**Code Reference:** `backend/semantic_search/services/query_parser_service.py`
```python
CONCEPT_TYPES = [
    "LOCATION", "OBJECT", "FIELD", "EVENT",
    "NETWORK", "AMOUNT", "PROPORTION",
]

def _extract_concepts(self, question: str, template: str) -> list:
    if self.concept_extractor is not None:
        X = self.vectorizer.transform([question])
        concept_vec = self.concept_extractor.predict(X)[0]
        heuristic_vec, heuristic_probs = self._heuristic_concepts(question, template)
        concept_vec = [int(a or b) for a, b in zip(concept_vec, heuristic_vec)]
```

---

## 3. Role Assignment and G2 Precedence Validation

### The Original Implementation
The MapQA/Spatial-Agent papers define a role hierarchy for DAG nodes. Each concept slot in a template is assigned a functional role (SUB_COND, COND, SUPPORT, MEASURE, EXTENT), and the DAG must satisfy constraint G2: roles must appear in precedence order.

### The Updated Implementation
The pipeline assigns roles via a multiclass `LogisticRegression` model trained on `question [CONCEPT:TYPE]` strings, then validates the resulting DAG against the G2 precedence constraint.

**Role Precedence (G2):**

| Role | Order | Description |
|------|------|-------------|
| `EXTENT` / `TEXTENT` | 0 | Spatial extent (defined, not used in 5 trained templates) |
| `SUB_COND` | 1 | Subject condition (the thing being queried) |
| `COND` | 2 | Condition (the spatial filter) |
| `SUPPORT` | 3 | Supporting evidence |
| `MEASURE` | 4 | Measurement (radius, count, distance) |

* **Mechanism:** `_ml_role_assignment()` in `query_parser_service.py` predicts a role for each concept using the trained model. `_compose_dag()` builds the DAG from template skeleton specs. `_validate_dag()` checks that roles appear in ascending `ROLE_ORDER`.
* **Transformation:** G1, G3, G4, G5 are satisfied by construction (template skeletons enforce them). G2 is checked at runtime on every parse.

**Code Reference:** `backend/semantic_search/services/query_parser_service.py`
```python
ROLE_ORDER = {
    "EXTENT": 0, "TEXTENT": 0,
    "SUB_COND": 1, "COND": 2,
    "SUPPORT": 3, "MEASURE": 4,
}

def _validate_dag(dag: list) -> dict:
    violations = []
    roles = [node["role"] for node in dag]
    for i in range(len(roles) - 1):
        if ROLE_ORDER.get(roles[i], 99) > ROLE_ORDER.get(roles[i + 1], 99):
            violations.append(f"G2: role {roles[i]} → {roles[i+1]} violates precedence")
    return {
        "valid": len(violations) == 0,
        "violations": violations,
        "constraints_checked": ["G2"],
        "constraints_satisfied_by_construction": ["G1", "G3", "G4", "G5"],
    }
```

---

## 4. DAG Composition (GeoFlow)

### The Original Implementation
The paper composes a GeoFlow DAG from the classified template, extracted concepts, and assigned roles. The DAG is a directed acyclic graph where each node represents a query operation (filter, aggregate, measure) and edges represent data flow.

### The Updated Implementation
The pipeline builds the DAG from fixed template skeletons stored in `TEMPLATE_SPECS`. Each skeleton defines a list of nodes with `role`, `concept_type`, and `operator`. The `_compose_dag()` method fills the skeleton with the extracted concepts and assigned roles.

* **Mechanism:** `template_specs` in `train_mapqa_parser.py` (lines 93-149) defines the skeleton for each of the 5 trained templates. `_compose_dag()` in `query_parser_service.py` (lines 464-500) instantiates the DAG by mapping roles to concepts.
* **Transformation:** The DAG is a list of node dicts, each with `role`, `concept`, `operator`, and `concept_type`. The executor walks this list to execute the query.

---

## 5. Executor: Template to PostGIS + pgvector

### The Original Implementation
The MapQA paper maps each template to a structured query over geospatial data. The executor translates the GeoFlow DAG into database operations.

### The Updated Implementation
The pipeline maps each template to a dedicated handler in `QueryExecutorService` that constructs PostGIS spatial queries and pgvector similarity queries.

**Template to Handler Mapping:**

| Template | Handler | Primary Operations |
|----------|---------|-------------------|
| #1 FILTER-AGGREGATE-MEASURE | `_execute_filter_aggregate_measure` | Geocode anchor → `ST_DWithin` radius → count |
| #2 OBJECT-FIELD-MEASURE | `_execute_object_field_measure` | Haversine distance between two geocoded points |
| #4 GEOCODE-BATCH-COMPARE | `_execute_geocode_batch_compare` | Nearest-amenity or compare-closer (multi-anchor) |
| #5 LOCATION-BEARING-CLASSIFY | `_execute_location_bearing_classify` | Bearing or cardinal cone search |
| #8 PLACE-ATTRIBUTE-QUERY | `_execute_place_attribute_query` | Heat kernel via `factor_spectral_node_metric` when available, else PostGIS fallback |
| #11 SPECTRAL-ANALYSIS | `_execute_spectral_analysis` | Factor-table backed (not parser-emitted) |
| #12 TEMPORAL-DRIFT | `_execute_temporal_drift` | Factor-table backed (not parser-emitted) |
| #13 COMMUNITY-DETECT | `_execute_community_detect` | Factor-table backed (not parser-emitted) |
| #14 EVENT-DIFFUSION | `_execute_event_diffusion` | Factor-table backed (not parser-emitted) |

### 3-Tier Amenity Fallback

The executor resolves amenity/object queries through a 3-tier fallback chain:

1. **Exact tag:** `tags__contains={"amenity": amenity_type}` (GIN-indexed JSONB containment)
2. **Ontology class:** `ontology.get_canonical_tags(wkg_class)` → `tags__contains` (WorldKG ontology lookup)
3. **FastText:** `gv_tags_embedding <=> query_embedding` with `_FASTTEXT_AMENITY_DISTANCE_THRESHOLD = 0.5` (pgvector cosine, exact via `+ 0`)

Generic-amenity phrases ("amenities", "places", "shops") are handled between tiers 2 and 3 via the GIN-indexed `tags ?|` operator (`_generic_amenity_keys`), never an embedding scan.

**Name-first pool tier (2026-09-28):** proper-name OBJECTs (brands) get a
4th, name-based tier merged into the FastText pool before the radius
filter (`_search_by_name` + `_merge_pools` in `spatial_search.py`): the
index-assisted trigram operator on `name_romanized` (sim ≥ 0.4, fragment
guard, `RomanizerRegistry` for cross-script) plus an exact-name fallback.
The embedding pool is name-blind for out-of-vocabulary brands — "juici
patties" admitted 1 entity, the name tier found 35 — so the name tier
decides pool membership and the re-rank decides order.

**Re-rank (name_score, 2026-09-28):** the executor re-ranks results by
`combined_score = diffusion_score + name_score + geo_score + uslp_boost`.
For proper-name OBJECTs, `name_score` is the FastText cosine between the
OBJECT span and each entity's `name` (exact brands ≈ 1.0, nameless 0.0);
category queries skip it. The re-rank now runs on #1
(FILTER-AGGREGATE-MEASURE) and #8 (PLACE-ATTRIBUTE-QUERY); previously only
#8. Without it, #1 returned the raw distance-sorted pool, burying exact
brand matches ("Island Grill" ranked below a nearer "cook shop").
See `DIFFUSION_SCORE_AND_QUERY_FUSION.md` for the fusion provenance.

**Code Reference:** `backend/semantic_search/services/query_executor_service/`
```python
# Spatial SQL construction
qs = qs.extra(
    where=["ST_DWithin(geom::geography, ST_MakePoint(%s, %s)::geography, %s)"],
    params=[float(anchor_point.x), float(anchor_point.y), float(radius_m)],
)
qs = qs.annotate(
    distance_m=RawSQL(
        "ST_Distance(geom::geography, ST_MakePoint(%s, %s)::geography)",
        ...
    )
).order_by("distance_m")

# pgvector FastText (exact cosine, not HNSW)
RawSQL("(gv_tags_embedding <=> %s::vector) + 0", (query_list,), ...)
```

---

## 6. Self-Supervised Training Data Generation

### The Original Implementation
The MapQA paper requires labeled (question, template, concepts) triples for training. Manual annotation is expensive and does not scale to new countries or snapshot dates.

### The Updated Implementation
The pipeline generates training data automatically from real OSM data using a self-supervised question generator and a rule-based paraphraser.

* **Question Generator:** `MapQAQuestionGenerator.generate(country_code, snapshot_id, templates=(1,2,4,5,8), per_class, ...)` in `backend/semantic_search/services/mapqa_question_generator.py`. Queries real entities from the snapshot, fills template slots with actual amenity names, locations, and radii, and produces labeled questions.
* **Paraphraser:** `MapQAParaphraser.paraphrase(question, slots)` in `backend/semantic_search/services/mapqa_paraphraser.py`. Rule-based slot-invariant paraphrasing with optional LLM augmentation. The slot-invariance gate ensures paraphrases preserve the same concept slots.
* **Training Pipeline:** `train_mapqa_parser` loads `mapqa_template_mapping.csv`, combines California, augmented, and self-supervised training data, holds out Illinois (zero-shot) and a self-supervised test set, and writes artifacts to `MAPQA_PARSER_DATA_DIR/artifacts/`.

---

## 7. Answer Enrichment (Direct Path vs Agent Path)

### The Original Implementation
The MapQA paper generates a natural-language answer from the executed query results. The Spatial-Agent paper extends this with a research loop where an LLM selects tools (nameSearch, structuredSearch) to gather additional context before synthesizing.

### The Updated Implementation
The pipeline implements two enrichment paths:

**Direct Path (default, deterministic):**
1. `EntityContextService.get_context(osm_ids, country_code, snapshot_date, template)` fetches deterministic context via 3 indexed SQL queries: USLP spatial links, factor-table communities, and class distribution.
2. `QueryEnrichmentService.synthesize(question, template, concepts, results, context, ...)` makes one LLM call grounded in the context digest.
3. Fail-soft: any LLM failure keeps the deterministic answer.

**Agent Path (MCP agents only):**
1. `QueryEnrichmentService.enrich(question, template, concepts, results, ...)` uses the old research loop: LLM selects `nameSearch`/`structuredSearch` tools via `chat_json`, executes them, then synthesizes.
2. This path is retained for MCP agent orchestration where an external agent (Goose, qwen3:8b) chains tool calls.

**Paper vs Code:** The direct path replaces the paper's research-tool selection loop with deterministic context fetches (~50ms-1.5s SQL), reducing LLM calls from 3 to 1 per enriched request. The agent path retains the original tool-selection pattern for MCP agents.

**Code Reference:**
- `backend/semantic_search/services/entity_context_service.py`: `get_context(cls, osm_ids, country_code, snapshot_date=None, snapshot_id=None, template=None, trace=None)`
- `backend/semantic_search/services/query_enrichment_service.py`: `synthesize(...)` (direct, lines 233-298) and `enrich(...)` (agent, lines 112-230)

---

## 8. SSE Streaming

The executor streams results to the frontend via Server-Sent Events:

**Event Sequence (Direct Path):**
```
event: parsed        → {template, concepts, roles, dag, confidence}
event: executed      → {template, result_count, trace}
event: context       → {context sources fetched}
event: answer_delta  → {token stream from LLM}
event: done          → {final result}
```

**Agent Path** additionally emits `research` and `research_out` events for tool calls.

**Code Reference:** `backend/worldkg_nca/views/search.py`, `execute_query_stream` view (lines 1111-1191). Plain Django view (`@require_GET`), DRF content-negotiation rejects `Accept: text/event-stream`. Pipeline runs in a worker thread; events flow through `queue.Queue` to `StreamingHttpResponse`.

---

## 9. HITL Confirmation Modal

### The Original Implementation
The Spatial-Agent paper describes a human-in-the-loop confirmation step where the user reviews and edits the parsed query before execution.

### The Updated Implementation
The pipeline implements HITL as a Pinia-driven Vue 3 modal:

* **Modal:** `QueryConfirmationModal.vue` renders `proposedQuery.template`, editable concept slots, and confidence score. Buttons: `Reject`, `Save Edits & Approve`, `Approve`.
* **Store:** `queryProposalStore.js` manages state: `idle | pending | approved | edited | rejected`. Actions: `proposeQuery`, `approveQuery`, `editQuery`, `rejectQuery`.
* **MCP Bridge:** `client-functions.ts` exposes `proposeQuery`, `getApprovalState`, `getQueryProposal` to the MCP server, allowing external agents to push proposals and poll for approval.

See `SPATIAL_AGENT.md` for the full MCP tool surface and agent orchestration flow.

---

## 10. Cross-References

| Topic | Document |
|-------|----------|
| Spatial-Agent MCP tool surface and agent orchestration | `SPATIAL_AGENT.md` |
| Kuhn core concepts mapping (paper vs code) | `KUHN_CORE_CONCEPTS.md` |
| GeoVectors embeddings (FastText amenity fallback tier 3) | `GEOVECTORS.md` |
| WorldKG enrichment (ontology class fallback tier 2) | `WORLDKG_ENRICHMENT.md` |
| USLP spatial links (EntityContextService context source) | `USLP.md` |
| Factor tables and spectral analysis (learned layer) | `docs/Schematics/05_Learned_Layer/` |
| MapQA parser operations flowchart | `docs/Schematics/05_Learned_Layer/08_MapQA_Parser_Operations_Flowchart.md` |
| MapQA geographic scoring and routing | `docs/Schematics/05_Learned_Layer/07_MapQA_Geographic_Scoring_and_Routing.md` |
