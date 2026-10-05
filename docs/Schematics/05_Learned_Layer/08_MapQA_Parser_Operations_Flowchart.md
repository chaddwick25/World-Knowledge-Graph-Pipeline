# MapQA Parser Operations Flowchart

> **Focus:** the full MapQA parser lifecycle: Phase A self-supervised
> training-data generation, Phase B offline training, Phase C runtime query
> path, and the self-supervision loop. Each phase produces concrete assets
> with explicit locations.
>
> **Key idea:** the parser is trained on the MapQA LLM dataset, a
> hand-curated augmentation CSV, and self-supervised rows generated from
> the platform's own `OsmEntity` ground truth. Illinois stays the zero-shot
> headline metric; the generated holdout is diagnostic only.

---

## 1. Phase A: Self-Supervised Training-Data Generation

**Source:** `semantic_search/services/mapqa_question_generator.py`,
`semantic_search/services/mapqa_paraphraser.py`,
`semantic_search/management/commands/generate_mapqa_training_data.py`

### 1.1 Problems the Expansion Solves

1. **Small min-class size**: #2 and #5 had only 300 examples each, all
   from 2 US regions.
2. **Geographic OOV**: every training entity name was California or
   Illinois; production queries hit Belize, Jamaica, Ireland, Korea. The
   parser had never seen those toponyms.
3. **Dataset quality bugs**: the raw dataset contained truncated entity
   names and **empty answers** (e.g. "What is the nearest car_pooling
   found west of Calico Ghost Town?" → empty). Self-supervision from the
   platform's own DB eliminates both.
4. **Lexical rigidity**: the MapQA-llm paraphrases came from one GPT-3.5
   pass; rule-based + Ollama paraphrase tiers add lexical variation.

### 1.2 Per-Template Sampling (Anchor-Relative)

All 5 templates are PostGIS-verifiable: the answer is computed exactly at
generation time and recomputed at test time.

| Template | Sampling / Ground truth / Answer shape |
|---|---|
| #1 FILTER-AGGREGATE-MEASURE | (amenity, named anchor, radius ∈ {50..500}m); `ST_DWithin(geog, anchor, r)` count → int |
| #2 OBJECT-FIELD-MEASURE | named pair (A, B) 0.5-200 km apart; haversine → distance_km (float) |
| #4 GEOCODE-BATCH-COMPARE | (a) triple (Z,X,Y) → argmin; (b) (amenity, Z); ordered distance, LIMIT 1 → entity name |
| #5 LOCATION-BEARING-CLASSIFY | (a) pair → bearing → cardinal; (b) cone ±45°; bearing formula → direction / entity name |
| #8 PLACE-ATTRIBUTE-QUERY | (a) named entity → amenity tag; (b) 50 m adjacency; tag lookup / ST_DWithin 50 m → amenity/name |

**Anchor-relative samplers (design deviation from the plan):** each sampler
picks the anchor first, then the amenity/radius/direction **from the set
that provably has entities nearby** (grouped `ST_DWithin` query), so
`count ≥ 1` / a nearest entity / a cone hit are guaranteed **by
construction**. The plan's "skip empty answers" rule becomes a safety net
instead of the primary control. Sparse-country yield went from ~0 rows
(#8b, BZ) to full budget.

### 1.3 Paraphrase Tiers

1. Seed question + slot values (entity names, radii, directions).
2. Two tiers:
   - **Rule-based (always)**: synonym maps (`within` ↔ `inside` ↔
     `no more than X from`; `nearest` ↔ `closest`; `amenity` ↔
     `facility`), voice/reorder forms.
   - **LLM tier (optional)**: Ollama via `LLMService`, gated by
     `MAPQA_LLM_AUGMENTATION_ENABLED`, 3-5 paraphrases per seed.
3. **Slot-invariance gate**: every slot value must appear verbatim. Slot
   drift is rejected (it breaks ground truth).
4. Output: extra CSV rows with `Question type =
   self_supervised_paraphrase`.

### 1.4 Class Distribution: Before / After (2026-08-25)

Scale: 1 block = 100 rows.

```
Template (#)                    before ---------> after
------------------------------------------------------
#1 FILTER-AGGREGATE-MEASURE       756 ████████-----> 1,511 ███████████████
#2 OBJECT-FIELD-MEASURE           300 ███----------> 1,500 ███████████████
#4 GEOCODE-BATCH-COMPARE        1,198 █████████████-> 1,500 ███████████████
#5 LOCATION-BEARING-CLASSIFY      300 ███----------> 1,500 ███████████████
#8 PLACE-ATTRIBUTE-QUERY          600 ██████-------> 1,454 ██████████████
------------------------------------------------------
TOTAL (all training rows)       3,154 ------------> 10,619
```

7,424 self-supervised rows appended (4,640 seeds + 2,784 rule-based
paraphrases; 41 hand-curated rows preserved) from BZ, JM, IE, KR.
**Min class size gate (≥ 1,000) met for all 5 templates.**

### 1.5 Train-Split Change (Required for the New Rows to Be Consumed)

```
train: california_full + augmented + self_supervised   (was: california + augmented)
test : illinois_test            (unchanged, stays the zero-shot headline)
hold : self_supervised_test     (new, stratified 20% of generated rows,
                                 per-class metrics in metrics.json, never trained on)
```

The loader tolerates both CSV schemas. Legacy 7-column rows get defaults
(`Source` derived from `Region`: mapqa-llm / hand-curated /
self_supervised).

### 1.6 Evaluation Gates: Actuals

| Metric | Gate | Actual (2026-08-25) |
|---|---|---|
| Illinois zero-shot accuracy | no drop > 2 pp vs 0.9852 | **0.9958** (improved; 4/948 errors) |
| Per-class F1 (existing 5) | ≥ 0.75 each | 0.9615 - 1.0 |
| Min class size after generation | ≥ 1,000 | 1,454 - 1,511 |
| Self-supervised holdout | reported (informational) | acc 0.9818, F1-macro 0.9819 (n=1,485) |
| `parse()` latency | < 50 ms | 1.4 - 3.2 ms |

### 1.7 Deviations from the Plan

- `natural_language_qa_pairs.csv` is **gitignored** (repo runtime-data
  convention). Generated rows live in the canonical mounted data dir but
  are not source-controlled.
- #8 seeds fell short of the 250/country budget (712/1,000). The
  named-with-amenity (#8a) + 50 m adjacency (#8b) constraints are the
  tightest; the class-balance cap keeps the class ≥ 1,000 regardless.
- **Planned follow-up**: USLP-weighted sampling prior
  (`docs/plans/USLP_WEIGHTED_QUESTION_GENERATION_PLAN.md`). Use USLP
  predicted links as a salience prior for *which* locations to sample
  (answers stay PostGIS-exact; USLP never becomes ground truth).

---

## 2. Phase B: Training (Offline)

Assets produced: pickle + JSON artifacts in `{MAPQA_PARSER_DATA_DIR}/artifacts/`.

**Inputs:**
- ASSET `mapqa_template_mapping.csv` (MapQA-llm raw dataset,
  `california_full` + `illinois_test`)
- ASSET `natural_language_qa_pairs.csv` (`augmented` hand-curated +
  `self_supervised` generated + `self_supervised_test` holdout)

**`train_mapqa_parser` → `_split_rows`:**
- train: `california_full` + `augmented` + `self_supervised`
- test: `illinois_test` (zero-shot, headline)
- hold: `self_supervised_test` (informational only)

**Three stages:**

| Stage | Model | Asset |
|---|---|---|
| Stage 1 | TF-IDF vectorizer (fit on train ONLY) + MultinomialNB template classifier | `vectorizer.pkl`, `template_classifier.pkl`, `label_encoder.pkl` |
| Stage 2 | OneVsRest LR concept extractor (multi-label, 7 concept types) | `concept_extractor.pkl` |
| Stage 3 | LR multi-class role assigner (6 functional roles) | `role_assigner.pkl`, `role_encoder.pkl` |

Stage 1 predictions run on Illinois + holdout → **ASSET `metrics.json`**:
`test_accuracy` (Illinois headline), `per_class` F1,
`self_supervised_holdout` {accuracy, f1_macro, per_class},
`self_supervised_samples`, `train_samples`, `test_samples`.

**Command-level artifacts (independent of the stages):**
- ASSET `template_specs.json`: 5 DAG skeletons (role/concept/operator per
  node)
- ASSET `amenity_vocab.json`: 629 amenity values (closed-vocab OBJECT
  match)

---

## 3. Phase C: Runtime Query Path

Outputs: parsed spec → execution trace → answer.

1. **User question** arrives.
2. **`QueryParserService.parse`:**
   1. TF-IDF transform → MultinomialNB → template + confidence
   2. Deterministic override: "within Xm/km of" → #1
   3. Concept extraction (trained model + heuristic safety net)
   4. LLM refinement (optional, low-confidence parses only)
   5. Role assignment → DAG composition → G2 validation
3. **OUT parsed spec**: `{template, concepts, roles, dag, confidence,
   validation}`
4. **`QueryExecutorService.execute`:**
   - template-specific executor
   - 3-tier amenity fallback: exact tag → ontology class → FastText
     semantic
   - spatial ops: `ST_DWithin`, distance ordering, factor tables (pgvector
     `<#>`), `EntityGeocoder` anchors
5. **OUT results + execution trace** (+ `latency_ms`), optional LLM
   enrichment → `enriched_answer`.
6. **Answer.**

---

## 4. The Self-Supervision Loop

1. `OsmEntity` (vectors) is sampled as anchors / answer entities.
2. **Generator** (samplers + paraphraser) computes answers exactly from
   PostGIS.
3. **ASSET CSV** `natural_language_qa_pairs.csv` is written; 20% holdout →
   `self_supervised_test`.
4. **Retrain** via `train_mapqa_parser`.
5. **Parser artifacts** + `metrics.json` are produced.
6. **Parse / execute** runs.
7. **Test-time recount**: answers recomputed independently from PostGIS
   (unit tests).
8. Loop repeats (same entities, same PostGIS truths).

**Loop invariants:**
- **Answers are computed exactly at generation time** (`ST_DWithin` counts,
  haversine, nearest-entity ordering, bearing→cardinal) and recomputed at
  test time. The unit suite asserts generated answers equal independent
  recounts (`test_mapqa_question_generator.py`).
- **Illinois remains the headline zero-shot metric**. The generated holdout
  (`self_supervised_test`) is reported for per-class diagnostics only, so
  generation can never leak into the "zero-shot" claim.
- **Determinism**: pool selection uses `md5(concat(osm_type, ':',
  osm_id::text))` ordering + a seeded `random.Random`. Same seed + snapshot
  → identical rows (regression-tested).
- **Idempotency**: re-running the command for the same
  (country, snapshot, template) replaces the previous batch. The CSV never
  grows on re-runs (regression-tested).

---

## 5. Generated Assets (Complete Inventory)

| Phase | Asset | Location | Producer | Notes |
|---|---|---|---|---|
| A | `natural_language_qa_pairs.csv` | `{MAPQA_PARSER_DATA_DIR}/training_data/` | `generate_mapqa_training_data` | 12-column schema (`Source`, `Pipeline_run_id`, `Snapshot_date`, `Ground_truth_table`, `Country_code`); idempotent merge keyed on `(Country_code, Snapshot_date, Macro-template)` |
| A | `self_supervised_test` rows | same CSV | `MapQAQuestionGenerator.split_holdout` | Stratified 20% per template; never trained on |
| B | `vectorizer.pkl` | `{MAPQA_PARSER_DATA_DIR}/artifacts/` | `train_mapqa_parser` Stage 1 | TF-IDF fit on train split ONLY (no leakage) |
| B | `template_classifier.pkl` | artifacts/ | Stage 1 | MultinomialNB, 5 classes |
| B | `label_encoder.pkl` | artifacts/ | Stage 1 | Template label ↔ index |
| B | `concept_extractor.pkl` | artifacts/ | Stage 2 | OneVsRest LR, 7 concept types |
| B | `role_assigner.pkl` + `role_encoder.pkl` | artifacts/ | Stage 3 | LR, 6 roles |
| B | `template_specs.json` | artifacts/ | Command | 5 DAG skeletons (nodes/roles/operators) |
| B | `amenity_vocab.json` | artifacts/ | Command | 629 amenity values (closed-vocab OBJECT matching) |
| B | `metrics.json` | artifacts/ | Stage evaluation | `test_accuracy` (Illinois headline), `per_class` F1, `self_supervised_holdout` (acc/F1/per-class), `self_supervised_samples` |
| C | parsed spec | in-memory | `QueryParserService.parse` | `{template, concepts, roles, dag, confidence, validation}` |
| C | results + trace | API response | `QueryExecutorService.execute` | template results, `latency_ms`, traced 3-tier fallback |
| C | enrichment | API response | `query_enrichment_service` | `primary_answer`, `actions`, `action_outputs`, `enriched_answer` (LLM) |
| C | `TaskResult` rows | default DB | Celery / endpoints | Executor audit trail (48h expiry) |
