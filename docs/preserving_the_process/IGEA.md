# Preserving the IGEA Model Architecture

How the neural alignment methodologies from the original [Iterative Geographic Entity Alignment (IGEA)](https://arxiv.org/abs/2105.00847) research paper were integrated into the Django pipeline.

---

## 1. Candidate Base Harvesting (Wikidata SPARQL)

### The Original Implementation
When aligning isolated local instances of OpenStreetMap (OSM) databases with global networks like Wikidata, the system must first locate candidate pairs. Generally, this uses asynchronous scripts that interface repeatedly with the Linked Open Data cloud.

### The Updated Implementation
The pipeline preserves this linkage heuristic via Python streaming SPARQL queries, managed locally inside PostgreSQL and Redis caching.
* **Mechanism:** `WikidataCandidateService` operates like the paper's candidate selector query logic, generating dynamic radius-bounded bounding boxes to filter Wikidata's `wdt:P625` coordinates.
* **Transformation:** By orchestrating `manage.py harvest_wikidata_candidates`, it populates a native internal representation of these candidates without running full unguided cloud sweeps.
* **Database Integration:** Uses `OSMWikiDataHierarchy` model for country metadata and BBOX resolution, eliminating hardcoded fallbacks.

**Code Reference:** `backend/worldkg_nca/services/wikidata_service.py`
```python
        query = f"""
        SELECT ?entity ?entityLabel ?location WHERE {{
          SERVICE wikibase:box {{
            ?entity wdt:P625 ?location .
            bd:serviceParam wikibase:cornerSouthWest "Point({min_lon} {min_lat})"^^geo:wktLiteral .
            bd:serviceParam wikibase:cornerNorthEast "Point({max_lon} {max_lat})"^^geo:wktLiteral .
          }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . }}
        }}
        LIMIT {page_size}
        OFFSET {offset}
        """
```

---

## 2. Neural String Cross-Attention (BiLSTM Alignment)

### The Original Implementation
A core contribution of the IGEA architecture is replacing generic Cosine Similarity (e.g., FastText or Levenshtein calculations) with dense Cross-Attention mechanisms. Neural Networks (specifically BiLSTM layers) encode contextual differences between naming conventions spanning different semantic graphs, learning cross-cultural semantic variations.

### The Updated Implementation
The pipeline replaces naive matching baselines with the exact math from the paper using a localized PyTorch `nn.Module`.
* **Mechanism:** `backend/igea/services/cross_attention_model.py` houses the `CrossAttentionIGEA` class.
* **Transformation:** A shared bidirectional LSTM encodes both the OSM label and the Wikidata candidate label. The tensors are then processed through cross-attention distribution math to learn specific correlation mapping.

**Code Reference:** `backend/igea/services/cross_attention_model.py` and `backend/igea/services/iterative_alignment_service.py`
```python
class CrossAttentionIGEA(nn.Module):
    """
    PyTorch BiLSTM Cross-Attention alignment layer as specified in IGEA.
    Replaces basic distance heuristics with neural correlation layers.
    """
    def forward(self, osm_seq, wd_seq):
        # BiLSTM Context Representation
        osm_context, _ = self.bilstm(osm_seq)
        wd_context, _ = self.bilstm(wd_seq)

        # Cross Attention Matching Matrix
        osm_proj = self.attention_weights(osm_context)
        attn_scores = torch.bmm(osm_proj, wd_context.transpose(1, 2))
        attn_weights = F.softmax(attn_scores, dim=-1)
```

---

## 3. Iterative Feedback Refinement

### The Original Implementation
The Iterative aspect (the "I" in IGEA) implies creating pairs, computing the confidence map of mappings, and iteratively passing these confidently mapped "matched pairs" back into the embedding graph space, fine-tuning the system.

### The Updated Implementation
By encapsulating the PyTorch layer inside `IterativeEntityAlignmentService`, the pipeline executes recursive match propagation within Django without heavy serialization. The cross-attention tensor distances are threshold-filtered (`ALIGNMENT_THRESHOLD = 0.5` constant; the Celery pipeline service passes `threshold=0.6`) and recursively linked to output deterministic entity equivalencies across global boundaries. `MAX_ITERATIONS = 3`.
