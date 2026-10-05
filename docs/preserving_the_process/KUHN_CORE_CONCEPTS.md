# Preserving Kuhn's Core Concepts of Spatial Information

How the theoretical framework from [Core Concepts of Spatial Information for Transdisciplinary Research](https://papers/WernerKuhn/Core-concepts-of-spatial-information-for-transdisciplinary-research.pdf) (Kuhn) was integrated into the MapQA parser and the pipeline's concept extraction layer.

---

## 1. Kuhn's Five Core Concepts

### The Original Framework
Kuhn's paper defines five core concepts of spatial information that form a transdisciplinary vocabulary for geographic reasoning:

| Concept | Definition | Spatial Question Answered |
|---------|-----------|--------------------------|
| **Location** | A position in geographic space, identified by coordinates or place names | "Where is X?" |
| **Neighborhood** | The vicinity or surrounding region of a location | "What is near X?" |
| **Field** | A continuous spatial phenomenon with a value at every location | "What is the value at X?" |
| **Object** | A discrete entity with identity, attributes, and spatial extent | "What is at X?" |
| **Network** | A set of connected entities with flow or relationships | "How are X and Y connected?" |

These concepts are designed to be language-independent and discipline-agnostic, providing a shared vocabulary for geographic information science.

---

## 2. How MapQA Uses Kuhn's Concepts

### The Paper's Approach
The MapQA paper uses Kuhn's core concepts as the feature space for concept extraction. Each natural-language question is decomposed into typed concept slots drawn from Kuhn's framework. The template skeleton defines which concept types are expected, and the concept extractor fills those slots with concrete values from the question text.

### The Implementation's Adaptation
The pipeline's concept vocabulary is derived from the MapQA/Spatial-Agent template requirements, not a literal Kuhn ontology. The `CONCEPT_TYPES` list in the parser defines 7 labels, of which 4 are active in the 5 trained templates.

**Concept Vocabulary (Implementation):**

| Implementation Label | Active in 5 Templates | Kuhn Mapping | Notes |
|---------------------|---------------------|--------------|-------|
| `LOCATION` | Yes | Kuhn **Location** | Place names, coordinates, anchor points |
| `OBJECT` | Yes | Kuhn **Object** | Amenities, entities being queried |
| `AMOUNT` | Yes | Not in Kuhn's 5 | Numeric quantities (count, radius) |
| `FIELD` | Yes (generic amenity) | Kuhn **Field** | Used for generic amenity attributes, not continuous phenomena |
| `EVENT` | No (vocabulary only) | Not in Kuhn's 5 | Defined but not active in any trained template DAG |
| `NETWORK` | No (vocabulary only) | Kuhn **Network** | Defined but not active in any trained template DAG |
| `PROPORTION` | No (vocabulary only) | Not in Kuhn's 5 | Defined but not active in any trained template DAG |

**Key Gaps:**
- **No `neighborhood` concept.** Kuhn's Neighborhood (vicinity, surrounding region) is not a distinct concept type. The pipeline handles "near" queries via the `RADIUS` value extracted as part of `AMOUNT` and the spatial `ST_DWithin` operation in the executor, not via a dedicated concept slot.
- **`NETWORK` is dormant.** Kuhn's Network (connected entities, flow) is in the vocabulary but not active in any of the 5 trained template DAGs. The spectral/graph templates (#11-14) that would use network structure are wired in the executor but not emitted by the parser.
- **`FIELD` is repurposed.** Kuhn's Field (continuous spatial phenomenon) is used in the implementation for generic amenity attributes (e.g., "amenities" as a field-like query), not for true continuous fields like elevation or temperature.
- **`AMOUNT` and `PROPORTION` are additions.** These are not in Kuhn's 5 concepts but are needed for MapQA template slots (radius, count, proportion queries).

---

## 3. Concept Extraction Mechanism

### The Original Approach
The paper uses Kuhn's core concepts as a multi-label classification problem: given a question, predict which concept types are present, then extract concrete values for each.

### The Updated Implementation
The pipeline uses `OneVsRestClassifier(LogisticRegression)` for multi-label concept detection, combined with heuristic regex extraction for concrete values.

**Two-Stage Extraction:**

1. **ML Stage:** `OneVsRestClassifier(LogisticRegression(class_weight="balanced", solver="liblinear", penalty="l2", C=1.0))` predicts a 7-label binary vector indicating which concept types are present in the question. Features are TF-IDF n-grams (1,2) of the question text.

2. **Heuristic Stage:** `_heuristic_concepts()` applies regex patterns to extract concrete values:
   - Radii: `(\d+)\s*(m|km|meter|kilometer)` → `AMOUNT` with numeric value
   - Place names: capitalized sequences, geocoded via `resolve_iso_code` or Nominatim
   - Amenity types: matched against known OSM tag keys (amenity, building, highway, etc.)

3. **Merge:** `concept_vec = [int(a or b) for a, b in zip(concept_vec, heuristic_vec)]` combines ML and heuristic predictions with logical OR.

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

## 4. Concept-to-Role Mapping

### The Original Approach
The Spatial-Agent paper assigns functional roles to concepts. Each concept slot in a template is assigned a role (SUB_COND, COND, SUPPORT, MEASURE, EXTENT) that defines its function in the query DAG.

### The Updated Implementation
The pipeline assigns roles via a multiclass `LogisticRegression` model trained on `question [CONCEPT:TYPE]` strings. The role determines the concept's position in the G2 precedence-validated DAG.

**Concept-to-Role Flow:**
1. Concept extraction produces a list of `(type, value)` pairs.
2. Role assignment predicts a role for each concept using the ML model.
3. DAG composition places concepts into the template skeleton based on their roles.
4. G2 validation checks that roles appear in ascending precedence order.

**Active Roles in the 5 Trained Templates:**

| Role | Order | Concept Types Typically Assigned | Template Usage |
|------|-------|----------------------------------|----------------|
| `SUB_COND` | 1 | `OBJECT` | The thing being queried (amenity, entity) |
| `COND` | 2 | `LOCATION` | The spatial filter (place name, anchor point) |
| `SUPPORT` | 3 | `LOCATION` (secondary) | Supporting location for multi-anchor queries |
| `MEASURE` | 4 | `AMOUNT` | Radius, count, distance |

`EXTENT` and `TEXTENT` (order 0) are defined but not used in the 5 trained templates.

---

## 5. Where Kuhn's Concepts Appear in the Code

### Direct References
Kuhn's name appears in the codebase only in:
- `README.md` (project description referencing the papers)
- `frontend-v3/src/mcp/mcp-server.ts` line 219: `"Kuhn's-template geospatial question"` (tool description for `templateQuery`)

### Indirect References
The concept vocabulary in `query_parser_service.py` and `train_mapqa_parser.py` is the primary indirect reference. The labels `LOCATION`, `OBJECT`, `FIELD`, `NETWORK` map to Kuhn's concepts by name, though the implementation's semantics differ (see §2).

### No Concept Registry
There is no separate concept ontology or concept registry in the codebase. The concept vocabulary is hard-coded in:
- `backend/semantic_search/services/query_parser_service.py` (`CONCEPT_TYPES`)
- `backend/semantic_search/management/commands/train_mapqa_parser.py` (`CONCEPT_TYPES`)

The `wkg_class` field on `OsmEntity` provides an ontological class distribution (from the WorldKG ontology), but it is not a Kuhn-concept registry. It serves the semantic axis (what something is), not the spatial-concept axis (how something is queried).

---

## 6. The Learned Layer as Network Concept Realization

### Kuhn's Network Concept
Kuhn's Network concept covers connected entities with flow or relationships. In the MapQA/Spatial-Agent papers, this would manifest as queries about connectivity, paths, or relationships between entities.

### The Implementation's Partial Realization
The pipeline's learned layer (Step 5c/5d) materializes network structure into factor tables, but the parser does not emit templates that use them:

**Factor Tables (Materialized Network Structure):**

| Table | Model | Kuhn Concept | Content |
|-------|-------|-------------|---------|
| `factor_spectral_node_metric` | `SpectralNodeMetric` | Network | Eigen loadings (128D), Fiedler component, Louvain community, Dirichlet contribution, degree, clustering coefficient |
| `factor_drift_node_metric` | `DriftNodeMetric` | Network (temporal) | Fiedler delta, loading drift, community change, degree delta |
| `factor_amenity_embedding` | `AmenityEmbedding` | Object (semantic) | 300D FastText embeddings per amenity type |
| `factor_subgraph_transport` | `SubgraphTransport` | Network (cross-subgraph) | Functional-map matrices between subgraph eigenbases |

**Executor Templates That Use Factor Tables (not parser-emitted):**

| Template | Handler | Factor Table Used | Kuhn Concept |
|----------|---------|-------------------|-------------|
| #11 SPECTRAL-ANALYSIS | `_execute_spectral_analysis` | `factor_spectral_node_metric` | Network |
| #12 TEMPORAL-DRIFT | `_execute_temporal_drift` | `factor_drift_node_metric` | Network (temporal) |
| #13 COMMUNITY-DETECT | `_execute_community_detect` | `factor_spectral_node_metric` | Network (community) |
| #14 EVENT-DIFFUSION | `_execute_event_diffusion` | `factor_spectral_node_metric` | Network (diffusion) |

**Paper vs Code:** The learned layer realizes Kuhn's Network concept through spectral graph theory (eigenvalues, heat kernel, community detection, diffusion), but the parser does not currently emit templates #11-14. These templates are wired in the executor and can be invoked directly, but the classifier does not route natural-language questions to them. The `EntityContextService` does use factor-table communities for answer enrichment on the direct path, providing indirect network context.

---

## 7. Spectral Analysis as Network Concept Math

### The Math
The learned layer computes spectral features of the k-NN graph built from entity locations:

**Normalized Laplacian:**
$$L = I - D^{-1/2} W D^{-1/2}$$

**Heat Kernel (diffusion from source node):**
$$H(t) = e^{-tL} \delta_s$$

**Spectral Drift (between snapshots):**
$$\text{drift} = \|\lambda_t - \lambda_{t-1}\|_2$$
$$\text{connectivity\_delta} = \Delta\lambda_2$$
$$\text{fiedler\_drift} = \text{cosine\_distance}(\mathbf{f}_t, \mathbf{f}_{t-1})$$

**Community Detection:** Louvain modularity maximization (`networkit PLM` or `NetworkX`).

**Code Reference:**
- `backend/semantic_search/services/spectral_analysis_service.py`: `compute_spectral_features(G, k=128)`, `compute_heat_kernel(G, source_node, t_values)`
- `backend/semantic_search/services/spectral_drift_service.py`: `compute_spectral_drift(fp_from, fp_to)`
- `backend/semantic_search/services/community_detection_service.py`: `detect_communities(G, resolution=1.0)`

**Solver Selection:** GPU `torch.lobpcg`, CPU shift-invert randomized SVD, or `scipy.sparse.linalg.eigsh` (auto-selected based on graph size and GPU availability).

---

## 8. Summary: Kuhn's Concepts vs Implementation

| Kuhn Concept | Implementation Status | Where |
|--------------|---------------------|-------|
| **Location** | Active | `LOCATION` concept type, geocoded via `resolve_iso_code` / Nominatim, spatial queries via `ST_DWithin` |
| **Neighborhood** | Not a distinct concept | Handled via `AMOUNT` (radius) + `ST_DWithin` spatial operation, not a dedicated concept slot |
| **Field** | Partially active | `FIELD` concept type used for generic amenity attributes, not true continuous fields |
| **Object** | Active | `OBJECT` concept type, matched via 3-tier amenity fallback (exact tag → ontology → FastText) |
| **Network** | Dormant in parser, active in learned layer | `NETWORK` concept type defined but not active in 5 trained templates. Spectral factor tables materialize network structure. Templates #11-14 wired in executor but not parser-emitted. |

**The core insight:** The implementation preserves Kuhn's Location and Object concepts directly, partially preserves Field (repurposed for amenity attributes), handles Neighborhood implicitly via spatial radius operations, and realizes Network through the spectral learned layer rather than the parser's concept vocabulary.

---

## 9. Cross-References

| Topic | Document |
|-------|----------|
| MapQA parser and concept extraction | `MAPQA.md` |
| Spatial-Agent MCP tool surface | `SPATIAL_AGENT.md` |
| Factor table math reference | `docs/Schematics/05_Learned_Layer/09_Factor_Table_Math_Reference.md` |
| Graph spectral drift | `docs/Schematics/05_Learned_Layer/04_Graph_Spectral_Drift.md` |
| External concepts DAG visualization | `docs/Schematics/05_Learned_Layer/05_EXTERNAL_CONCEPTS_DAG_VISUALIZATION.md` |
| Spectral filtering (Chebyshev vs Lanczos) | `docs/Schematics/05_Learned_Layer/06_Spectral_Filtering_Chebyshev_vs_Lanczos.md` |
| Inductive BallTree IDW | `docs/Schematics/05_Learned_Layer/03_Inductive_BallTree_IDW.md` |
