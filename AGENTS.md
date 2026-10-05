# World Knowledge Graph Pipeline — Agent Notes

## Architecture (post-refactor)

The pipeline has been refactored into a modular, service-oriented architecture:

### OSMSnapshot app (snapshot identity + graph data)

The `osmsnapshot` app is the canonical home for snapshot identity and graph data:

- **`Snapshot`** — A history-flattened OSM extract pinned to a moment in time for a country.
  Identity: `(country_code, snapshot_date)`. Created by `SnapshotExtractionService`
  after osmium extract + time-filter. FK to `core.PbfFile`.
- **`SnapshotJob`** — DB-backed pipeline status tracking (moved from `orchestration`).
  Tracks `(snapshot_date, country_code)` processing status. Frontend queries this for
  year selector badges.
- **`WorldKGClassDrift`** — Ontological class distribution drift between two snapshots
  (moved from `analysis`). FKs to `Snapshot`.
- **`WorldKGClassFingerprint`** — Monthly ontological fingerprint for a region
  (moved from `analysis`). FK to `Snapshot`.
- **`PbfTagDistribution`** — Tag distributions for PBF files (moved from `analysis`).

**Deleted** (from the former `analysis` app):
- `TemporalSnapshot` — Legacy snapshot model. Replaced by `Snapshot` (`country_code` + `snapshot_date`).
- `AssetBundle` — Legacy asset bundle model. Replaced by `GraphExtract` (both removed 2026-09-09 — the parquet graph-substrate layer was dormant; no pipeline code created `GraphExtract` rows or invoked `AssetExtractor`).
- Legacy services: `FilteredSnapshotService`, `OptimizedSnapshotService`, `ParallelSnapshotService`, `GraphAssetParallelService`, `GraphAssetService`, `AssetGenerationService`.
- Legacy endpoints: `/api/tag-discovery/analyze/`, `/api/assets/bundles/`, `/api/country-search-update/`, filtered snapshot views.

### Core app (consolidated from `extraction` + `orchestration`)

The `extraction` and `orchestration` Django apps have been **deleted**. All their
models, services, and management commands now live in the `core` app. Planet
initialization (formerly a Celery canvas of `step_0a`–`step_0m` tasks) is now a
single `init_planet` management command run as a Docker entrypoint step after
migrations. See `docs/plans/completed/CORE_APP_CONSOLIDATION_PLAN.md`.

**Models** (`core/models/`, all 29 models, app_label `core`, db_tables unchanged):
- `extraction.py` — `PbfFile`, `RegionHierarchy`, `OsmBoundary`, `PbfExtract`,
  `PolygonFile`, `PbfCollection`, `RegionalExtractionState`, `ProjectionWeightAsset`,
  `PlanetaryMetrics`
- `hierarchy.py` — `OSMWikiDataHierarchy`
- `infrastructure.py` — `ProcessingSession`, `Task`, `OsmiumDatasetMetrics`,
  `RegisteredService`, `PlanetSnapshot`, `CountrySearchProcessing`
- `pipeline.py` — `PipelineRun`, `PipelineLogEntry`, `PipelineAsset`
- `country_profile.py` — `CountryRelationSnapshot`, `ContinentProfile`,
  `CountryPipelineProfile`, `SubgraphProfile`, `EligibleCountry`
- `artifact.py` — `CountryArtifact`, `EmbeddingArtifact`, `PartitionRegistry`,
  `ReasoningWorkflow`, `ReasoningStep`

**Services** (`core/services/`):
- `planet_init/` — Planet init services (formerly `extraction/services/`):
  `planet_initialization_service`, `osm_relation_hierarchy_service`,
  `geofabrik_index_service`, `geofabrik_poly_service`, `country_relation_resolver`,
  `sparql_country_relation_service`, `hierarchy_cache_service`,
  `places_enrichment_service`, `non_sovereign_territories`, `pbf_hierarchy_resolver`,
  `extraction_chain_builder`, `pbf_cache_manager`, `polygon_geojson_service`,
  `geometry_utils`, `country_metadata`, `osm_wikidata_resolver`
- `snapshot/` — Snapshot + embedding services (formerly `extraction/services/`):
  `snapshot_extraction_service`, `embedding_service`, `embedding_split_service`,
  `embedding_merge_service`, `embedding_spatial_split_service`,
  `embedding_shapely_splitter`, `gb_uk_copy_service`, `temporal_extract_service`,
  `subgraph_pbf_service`, `subgraph_list_service`, `region_status_service`,
  `osmium_facade`, `regional_path_service`,
  `country_override_service`, `extraction_service`, `pbf_bounding_box_service`
- `pipeline/` — Pipeline services (formerly `orchestration/services/`):
  `search_ready_service`, `search_update_orchestrator`, `base_service`,
  `artifact_service`, `artifact_facade`, `drive_sync_verifier`

**Management commands** (`core/management/commands/`): 19 commands moved from
`extraction/` and `orchestration/`, plus the new `init_planet.py` (Docker
startup command that replaces the planet init Celery canvas).

**Planet init** (`init_planet` command):
- Runs as a Docker entrypoint step on the backend container after `migrate`
  (gated by `RUN_INIT_PLANET=true`; worker has `RUN_INIT_PLANET=false`).
- Idempotent — uses `PlanetSnapshot` as a soft lock; short-circuits if a
  COMPLETED row exists for today (pass `--no-lock` to force re-run).
- 16 steps: `register_planet`, `extract_continents`, `sync_hierarchy`,
  `prebuild_country_paths`, `prebuild_subgraphs`, `prebuild_wikidata_ids`,
  `copy_gb_to_uk`, `scan_embeddings`, `split_embeddings`, `merge_us_embeddings`,
  `rescan_embeddings`, `enrich_worldkg_classes`, `generate_osm_boundaries`,
  `train_mapqa_parser`, `compute_amenity_embeddings`,
  `finalize`. Run `python manage.py init_planet --step <name>` for a single step.
- Best-effort — failed steps log warnings but don't abort the chain (matches
  the old Celery chain's `max_retries=1` semantics).
- API: `GET /api/system/status/` reports readiness (derived from the latest
  *finalized* `PlanetSnapshot` — `snapshot_date_str` populated + COMPLETED)
  plus home-page hydration data (suggested planet path, extraction/embedding
  counts, snapshot-date range). The legacy `POST /api/planet/initialize/`
  (410 GONE) and `GET /api/planet/status/<id>/` facades were removed.

**Deleted Celery tasks** (planet init canvas):
- `step_0_initialize_planet`, `step_0b_initialize_continent`,
  `step_0c_prebuild_structure`, `step_0d_prebuild_country_paths`,
  `step_0e_prebuild_subgraphs`, `step_0f_prebuild_wikidata_ids`,
  `step_0h_scan_embeddings`, `step_0h_copy_gb_to_uk`,
  `step_0i_prebuild_split_embeddings`, `step_0j_prebuild_merge_us_embeddings`,
  `step_0k_rescan_embeddings`, `step_0l_enrich_worldkg_classes`,
  `step_0m_generate_osm_boundaries`, `_finalize_planet_init_chain`.
- `pipeline/canvas.py` keeps `run_planet_initialization` / `run_continent_initialization`
  as thin facades that delegate to `init_planet` (synchronous, no Celery canvas).
- `pipeline/tasks/__init__.py` only re-exports Steps 1–6 + Step 5c/5d (country pipeline). Steps 5c (graph spectral analysis) and 5d (temporal drift) run after Step 5/5b and before Step 6. Both are non-fatal/conditional. **Step 5c routing** is node-count-based: countries with < 5M nodes (`SUBDIVISION_NODE_THRESHOLD`) use country-level GPU LOBPCG (one global eigenbasis); countries with ≥ 5M nodes use subdivision-scoped solves + functional-map transport matrices (Phase B). Falls back to country-level with a warning if ≥ 5M but no subgraphs configured. Both paths are sequential (one solve at a time) and automatic (routing based on live `OsmEntity` count). **GPU resource management**: VRAM pre-check (`_check_gpu_vram` — estimates needed VRAM, falls back to CPU `eigsh` if insufficient with 20% safety margin), aggressive cleanup between subgraphs (`synchronize()` + `empty_cache()` × 2 + `gc.collect()`), `PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:128` in worker env. Writes per-entity `factor_spectral_node_metric` rows (with `subgraph_slug` for subdivision-scoped analysis) for SQL-only runtime factor resolution. GraphML is retained as a debug artifact but no longer read at request time. See `docs/plans/completed/STEP_5C_SUBDIVISION_SPECTRAL_PLAN.md` and `docs/plans/completed/SUBGRAPH_FUNCTIONAL_MAPS_PLAN_v2.md`. BZ subdivision validated 2026-08-20: 5/5 subgraphs, 10/10 transport matrices, 32,402 factor rows, 0 failures.

**Backward compatibility**: `api/models.py` re-exports all models from `core.models`
and `osmsnapshot.models`. Existing `from api.models import X` imports continue to work.

**Fresh migrations**: Both `django_db` and `vector_db` were dropped and recreated
during the consolidation. `core.0001_initial` (29 models) and
`osmsnapshot.0001_initial` (6 models) are the only migrations for the consolidated
apps. `django_migrations` rows for the deleted `extraction`/`orchestration` apps
were removed.

### Service Classes (data plane)
- `core/services/snapshot/gb_uk_copy_service.py` — Copy great-britain TSVs to united-kingdom naming
- `core/services/snapshot/embedding_split_service.py` — Split multi-country TSVs (GB, MY/SG/BN)
- `core/services/snapshot/embedding_merge_service.py` — Merge US regional shards
- `core/services/snapshot/embedding_service.py` — Embed OSM entities (Step 1, **FastText-only** — the two-axis `DualEncodingWriter`/`_run_parallel_dual` path was removed 2026-09-10, see `docs/issues/TICKET_REMOVE_DUAL_ENCODER.md`; GV-NLE comes from Step 5 training + the inductive query-time path). Uses `_run_parallel` (Approach B fan-out) when `PARALLEL_UPSERT_WORKERS > 1` (**code default 1 — single-threaded unless enabled**); legacy single-threaded `DBOnlyWriter` path when set to 1. Resolves `Snapshot` UUID and passes `source_snapshot_id` to `VectorStorageService`.
- `backend/backend/management/commands/verify_paths.py` — Boot-time path/DB validator. Since 2026-09-11 it also loads `OVERRIDES_JSON_PATH` and validates every `embedding_splits.splits[].targets[].poly` (overrides/ then root resolution order) plus split output parent dirs; malformed sections warn; `--strict` exits 1 on missing override file/polys.
- `core/services/snapshot/snapshot_extraction_service.py` — Extract country PBF from continent PBF via osmium. Creates `PbfFile` + `Snapshot` rows.
- `geovectors_encoder/services/batch_collector.py` — Parallel encode + upsert fan-out (Approach B): `BatchCollector` (drop-in writer replacement), `encoding_worker`, `spawn_encode_workers`, `ErrorBox`. See `docs/plans/completed/PARALLEL_UPSERT_APPROACH_B_PLAN.md`.
- `geovectors_encoder/services/vector_storage_service.py` — SQL COPY upsert into OsmEntity. Now sets `source_snapshot_id` (Snapshot UUID) on every upserted row.
- `igea/services/igea_pipeline_service.py` — Run IGEA alignment
- `geovectors_encoder/services/gv_nle_training_service.py` — Train GV-NLE (DeepWalk)
- `semantic_search/services/spectral_analysis_service.py` — Laplacian eigendecomposition (scipy sparse `eigsh`), heat kernel diffusion. Called by Step 5c.
- `semantic_search/services/graph_signal_service.py` — WorldKG class signals, Dirichlet energy `sᵀLs`, signal diffusion via `(L + μI)⁻¹s`. Called by Step 5c.
- `semantic_search/services/community_detection_service.py` — Louvain community detection (NetworkX), class-filtered communities. Called by Step 5c and API.
- `semantic_search/services/spectral_drift_service.py` — Spectral distance `‖λ_t - λ_{t-1}‖₂`, Fiedler drift, drift classification (low/medium/high/extreme). Called by Step 5d.
- `semantic_search/services/spectral_forecast_service.py` — ARIMA(1,1,1) eigenvalue forecast, exponential smoothing fallback, CUSUM change-point detection. Called by Step 5d.
- `semantic_search/services/embedding_drift_service.py` — Sliced Wasserstein Distance between snapshot embeddings, freshness score. Management command only.
- `semantic_search/services/knn_graph_service.py` — `build_graph()` builds weighted NetworkX graph with `wkg_class` node attributes for spectral/community analysis. `build_sparse_graph()` returns a PyG-style COO `SparseGraph` for large graphs. `build_sparse_graph_for_subgraph()` filters entities by subgraph polygon/bbox with a buffer zone for correct boundary k-NN behavior (used by Step 5c subdivision fan-out). With `return_core_ids=True` (Option A from the functional maps plan), returns `(SparseGraph, core_ids)` where the graph includes all buffered entities (core + buffer) and `core_ids` is used to prune at factor-write time — gives shared buffer entities exact eigen-loadings for transport matrix computation.
- `semantic_search/services/factor_node_writer.py` — Batch writer for per-entity factor tables (`factor_spectral_node_metric`, `factor_drift_node_metric` on vectors DB, models in `worldkg_nca`). Called by Steps 5c/5d (non-fatal). `write_spectral_nodes` accepts an optional `subgraph_slug` for subdivision-scoped rows, an optional `core_ids` set for Option A (full buffered graph solve — factor rows written for core entities only), and an optional `fingerprint_id` stamped on every row (Phase 1 eigenbasis coherence). Drift writer sign-aligns eigenbases across snapshot pairs. See `docs/plans/completed/FACTOR_NODE_RUNTIME_JOINS_PLAN.md` §10 and `docs/plans/completed/STEP_5C_SUBDIVISION_SPECTRAL_PLAN.md`.
- `semantic_search/services/factor_resolution_service.py` — Runtime SQL-only factor resolution: `check_availability` (G4), `resolve_metrics`, `diffusion_rank` (heat kernel as one pgvector `<#>` query over stored eigen-loadings, subgraph-scoped when the anchor has a `subgraph_slug`; supports cross-subgraph transport via functional maps when `cross_subgraph=True` and transport matrices exist in `factor_subgraph_transport`), `community_summary` (optional `subgraph_slug` filter), `amenity_embedding`. Used by the MapQA executor (table path is authoritative; `FACTOR_NODE_TABLES_ENABLED` defaults `true`).
- `semantic_search/services/subgraph_transport_service.py` — Functional map (k×k transport matrix) computation and runtime transport between adjacent subgraph eigenbases. `compute_transport_matrix()` solves regularized least squares (descriptor + Laplacian commutativity). `transport_loadings()` applies the matrix at runtime. `compute_adjacency()` detects adjacent subgraph pairs via polygon/bbox intersection. `write_transport_matrices()` / `load_transport_matrix()` handle vectors DB I/O. Grounded in Ovsjanikov et al. 2012, Pegoraro et al. 2023. See `docs/plans/completed/SUBGRAPH_FUNCTIONAL_MAPS_PLAN_v2.md`.
- `semantic_search/management/commands/compute_amenity_embeddings.py` — Precompute FastText embeddings for the MapQA amenity vocab (621 rows in `factor_amenity_embedding`). Registered as an `init_planet` step after `train_mapqa_parser` and before `finalize`.
- **Test infra**: pytest needs `CREATE EXTENSION vector` in BOTH test DBs (`test_vector_db` on postgres-vectors — pre-create with `OWNER vector_user` — and `test_django_db` on postgres-default). Kuhn's Template correction tests additionally need `pg_trgm` + `fuzzystrmatch` on `test_vector_db` (migrations 0017/0019 create them on the live DBs; `--no-migrations` means test DBs need them created once by hand: `CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;`). DB tests using vectors need `pytest.mark.django_db(transaction=False, databases=["default", "vectors"])`. The legacy runtime graph path has been removed; factor tables are the authoritative source for spectral/diffusion/community queries.
- `semantic_search/services/query_parser_service.py` — MapQA parser: TF-IDF + MultinomialNB template classifier, one-vs-rest Logistic Regression concept extractor, Logistic Regression role assigner, DAG composition. OBJECT extraction is open-vocabulary (returns raw phrase before first structural preposition). Includes a deterministic post-classification override: queries matching `within X(km|m) of` are forced to `FILTER-AGGREGATE-MEASURE (#1)` regardless of TF-IDF classifier output, preventing radius-bearing queries from being misrouted to PLACE-ATTRIBUTE-QUERY (heat kernel diffusion with no geographic radius filter). **Question-word typo tolerance (2026-09-13)**: `_normalize_question_words` rewrites near-miss question words ("Whichs" → "Which") via prefix-constrained edit distance (the question word must be a prefix of the token or vice versa — excludes "Mill"→"Will"/"What"→"Who"), applied in `extract_all_entities`/`_extract_entity_name`; `_extract_entity_name` anchors on the EARLIEST preposition with a ":" span terminator so "...to Moher Cottage: ...the Cliffs OF Moher?" anchors on Moher Cottage, not "Moher". **Heuristic OBJECT back-in vocabulary (2026-09-19)**: the trained concept model misses rare amenity words, and the `_heuristic_concepts` OBJECT signature list lacked the documented `sample_questions.md` vocabulary — "Which museums are within 2km of Belfast?" extracted no OBJECT, so the executor skipped the search entirely and research summaries reported "no museums found". The list now carries museum/beach/park/supermarket/gas station/bakery/library/cinema/clinic/church/university/gallery/theatre/theater/stadium/swimming/playground/brewery/distillery (substring matching covers plurals). See `docs/issues/resolved/BELFAST_ANCHOR_SIGNPOST_BUG.md`.
- `semantic_search/services/query_executor_service.py` — MapQA executor: 5 template executors using the factor-table path (SQL + pgvector `<#>` for spectral/diffusion/community queries) with PostGIS fallback for distance/radius templates. **Partition-pruned context + NaN guard (2026-09-13)**: result dicts carry `country_code`; the enrichment block resolves an `effective_country` from the results when the request has none (class-distribution context query 12.4s → ~0.3s); compare-closer candidate geocodes prune to the anchor's country with an unpruned cross-country retry (executor fn 6.9s → 0.22s); `_search_by_amenity_spatial` excludes `POINT(NaN NaN)` geometries (`ST_Distance` on NaN returns 0 — the "nearest (0m away)" regression). Endpoint on the Moher Cottage compare question: 40.4s → 9.0s. The legacy runtime graph path (GraphML → NetworkX → SciPy) has been removed. 3-tier amenity fallback: exact tag → WorldKG ontology class → FastText semantic (checks `factor_amenity_embedding` first). Also handles 4 graph endpoints (spectral, temporal, community, event-diffusion). Radius parsing supports km/m units (`"2km"` → 2000m). PostGIS `_search_by_amenity_spatial` applies haversine filtering using anchor coordinates. Returns empty list with `spatial_filter_skipped` trace when anchor has no usable coordinates and a radius was requested (no silent unfiltered fallback). **USLP geographic scoring** (Mann et al. 2023 §3.3): `_geo_score_uslp()` encodes anchor and candidate coordinates to geohash at P4 precision (~39km cells), computes haversine between cluster centers, normalizes by d_max (P4 cell width fallback when no precomputed pool is available). FILTER-AGGREGATE-MEASURE with an explicit radius uses raw haversine with the user's radius as d_max. OBJECT-FIELD-MEASURE returns 0.0 (distance IS the answer). **Radius guard**: PLACE-ATTRIBUTE-QUERY executor filters heat kernel results by haversine distance when an AMOUNT concept is present, preventing graph-connected but geographically distant entities from appearing. **USLP signal boost**: entities that appear as predicted link tails (`SpatialTripletScore.predicted=True`) from the anchor entity get a +0.5 score boost, connecting the link prediction layer to search ranking. `_enrich_with_geo_and_uslp()` applies geo_score + USLP boost and re-ranks by `combined_score = diffusion_score + geo_score + uslp_boost`.
- `semantic_search/services/entity_geocoder.py` — Geocodes named entities from MapQA LOCATION/OBJECT slots. Strips leading articles (`"a bus station"` → `"bus station"`) before ILIKE name search. Treats `POINT(NaN NaN)` way geometries as no-coordinate (returns `lat=None, lon=None` via `math.isnan` check). Prefers entities with valid coordinates over ways with NaN geom (iterates up to 20 ILIKE matches, returns first with usable lat/lon, falls back to no-coordinate match only as last resort). **Fragment guards (2026-09-13)**: tier 1 (pg_trgm) rejects candidates with sim < 0.6 whose name is > 1.6x the query; tier 3 (icontains) applies the same for queries < 8 chars — a truncated span ("Cliffs") can no longer match "Cliffs of Howth" while the equal-length typo case ("Shannon Bells" → "Shandon Bells", sim 0.65) and partial names ≥ 8 chars are preserved. `_entity_to_dict` carries `country_code` so the executor can prune partition queries. **Place preference (2026-09-19)**: all four tiers now select via `_pick` (prefer coordinates, then `_place_score` — settlements/admin boundaries up, marker/sign entities down) instead of `.first()` — "Belfast" the city beats "Belfast" the NCN 93 destination sign, which previously won the exact-name tie and sent every Belfast-anchored question radiating from a countryside signpost. See `docs/issues/resolved/BELFAST_ANCHOR_SIGNPOST_BUG.md`.
- `worldkg_nca/views/search.py` — `worldkg_semantic_triplet_search` API: triple-space and ANN search paths over `OsmEntity.gv_tags_embedding` (300D FastText). **Tag key filtering**: when `query_tags` has specific keys, the queryset is always filtered to entities that have those tag keys present (`tags__has_key`), preventing entities without the tag from polluting results. `exact_tag_match=True` additionally requires exact key=value match. **Tag match boost**: +1.0 to `final_score` for each query tag value that exactly matches the entity's tag value, ensuring `{"cuisine": "jamaican"}` ranks `cuisine=jamaican` above `cuisine=indian`. **USLP geo_score** (Mann et al. 2023 §3.3): both ANN and triple-space paths use geohash P4 cluster-center distance with d_max=39km (P4 cell width), replacing the old `1/(1+d_km)` decay. Also handles `natural_query` (free-form name search with romanizer cross-script matching) and `execute_query` (MapQA parser → template → executor). The three query modes (Structured JSON, Natural language, Kuhn's Template) are independently testable and serve as the agent's tool palette for future MCP orchestration — see `docs/plans/VUE_MCP_SERVER_OVERVIEW.md` §8.
- `semantic_search/services/query_correction_service.py` — **QueryCorrectionService** (Kuhn's Template correction layer): corrects misspelled/mistyped amenity phrases that break the MapQA executor's exact tiers ("resturant" → "restaurant"). Scores the snapshot's real OSM amenity tag vocabulary (`DISTINCT tags->>'amenity'`, cached per (snapshot, country), TTL 300s) with pg_trgm `similarity()` + fuzzystrmatch `levenshtein()` in one SQL pass; tiers: exact pass → pg_trgm (sim ≥ 0.5) → fuzzystrmatch (≤ 3 edits), combined score floor 0.55; junk and open-vocabulary cuisine phrases are never force-corrected (fall through to the FastText tier). Wired into `QueryExecutorService._resolve_amenity_tag`, `_search_by_amenity` (step 2b, trace `match_type="fuzzy_correction"`) and `_amenity_candidate_osm_ids`. Scoped to Kuhn's Template (`execute-query/`) — structured-JSON and natural-name modes are unaffected. Suite: `tests/unit/test_query_correction_service.py` (18 tests).
- `semantic_search/services/romanizing_names/` — Deterministic cross-script name romanization. `RomanizerRegistry.auto_romanize()` detects script (Hangul, diacritic Latin, identity) and dispatches to the appropriate romanizer. Hangul: Unicode decomposition + lookup tables (파리바게뜨 → paribagetteu). Diacritic: NFKD + combining mark removal (café → cafe, Señor → senor). Identity: lowercase + whitespace collapse fallback. No neural model, no network, pure Python stdlib. Romanized values persisted in `OsmEntity.name_romanized` with pg_trgm GIN index. `romanize_names` management command is idempotent/resumable (processes entities where `name_romanized IS NULL`), decoupled from country ingestion. Chinese Hanzi and Japanese Kanji intentionally excluded. See `docs/Schematics/07_Romanizer/01_Deterministic_Cross_Script_Romanization.md`.
- `core/services/pipeline/search_ready_service.py` — Mark country as search-ready + update SnapshotJob status

### Envelopes (message plane)
- `pipeline/envelopes.py` — `CountryEnvelope`, `PlanetEnvelope`, `ModelHyperparams`
- `pipeline/hyperparams.yaml` — Static model hyperparameters (loaded once, frozen)

### Decorator (control plane)
- `pipeline/task_decorator.py` — `@pipeline_step(step_name, EnvelopeClass, step_index)`
  Factors out the Celery seam: context setup, logging, WS push, serialization.

### Tasks (thin wrappers)
All task files in `pipeline/tasks/` are now thin wrappers that:
1. Reconstruct the envelope from the config dict
2. Call a service
3. Return the envelope (serialized via `to_dict()`)

### Legacy
- `pipeline/config.py` — `SubgraphConfig` + `_resolve_country_pbf_path` only; the
  legacy `CountryConfig` class has been deleted. Use `CountryEnvelope` /
  `PlanetEnvelope`.
- `TemporalOrchestratorService`, `ContinentSnapshotService`, `step_0g_continent_snapshots` — Deleted. Replaced by `SnapshotExtractionService` + DB-backed `SnapshotJob` model.
- `DailySnapshotService`, `HierarchicalPreprocessingOrchestrator`, `temporal_analysis_urls.py`, `sync_country_hierarchies` command — Deleted (orphaned, zero references).
- `WorldKGPipelineService` (`worldkg_nca/services/pipeline_orchestrator.py`) — Removed. `_resolve_iso_code()` consolidated into `core.services.planet_init.osm_wikidata_resolver.resolve_iso_code()`. `ISO_BBOX_FALLBACK` eliminated.
- `analysis` app — **Deleted.** `TemporalSnapshot` and `AssetBundle` models removed. Drift/fingerprint/PbfTagDistribution models moved to `osmsnapshot`. Legacy tag-discovery, asset-bundle, filtered-snapshot, and country-search-update endpoints and their backing services deleted.
- `GraphExtract` + `AssetExtractor` — Removed 2026-09-09 (dormant parquet graph-substrate layer; nothing created `GraphExtract` rows or invoked `AssetExtractor`).
- `source_snapshot_id` provenance — Fixed: `region_status_service.py` was using `PbfFile` UUIDs instead of `Snapshot` UUIDs. `EmbeddingService` now resolves the `Snapshot` UUID and passes it through the entire upsert chain (`VectorStorageService` → `BatchCollector` → `spawn_encode_workers`).
- **Done**: `extraction` and `orchestration` apps consolidated into the `core` app, with planet init converted to a Docker startup step (`init_planet` command). See `docs/plans/completed/CORE_APP_CONSOLIDATION_PLAN.md` and the "Core app" section above.

### SnapshotJob (pipeline status tracking)
- `osmsnapshot/models.py` — `SnapshotJob` model (country_code, snapshot_date, status, pipeline_run_id FK)
  (Moved from `orchestration` to `osmsnapshot`. DB table `snapshot_jobs` unchanged.)
- Pipeline start view gates on SnapshotJob: 409 if RUNNING, `force=true` to re-run COMPLETED
- Step 6 + canvas `on_failure` update SnapshotJob.status
- REST: `/api/snapshot-jobs/<iso>/` (dates), `SnapshotJobStatusView`, `SnapshotJobResultsView`
- `OsmEntity.source_snapshot_id` (UUID) — cross-DB reference to `osmsnapshot.Snapshot`
- `OsmEntity.snapshot_id` (CharField `YYYY_MM_DD`) scopes entity queries; data endpoints accept `snapshot_date` param

### Factor-Node Tables (worldkg_nca, vectors DB)

Three flat pgvector tables in the `worldkg_nca` app (routed to `vectors` DB by `VectorDBRouter`, colocated with `OsmEntity` for runtime joins). Batch-written by Steps 5c/5d + `compute_amenity_embeddings`; resolved at runtime by `FactorResolutionService` via SQL + pgvector `<#>`. See `docs/Schematics/05_Learned_Layer/05_Factor_Node_Runtime_Joins.md`.

- **`factor_spectral_node_metric`** — One row per `(snapshot_id, country_code, subgraph_slug, osm_id)`. Fields: `fingerprint_id` (UUID, null — the GraphSpectralFingerprint.id whose eigenbasis produced the loadings; Phase 1 coherence check at runtime, mismatch → PostGIS fallback), `eigen_loadings` (VectorField 128D, zero-padded), `fiedler_component`, `louvain_community`, `dirichlet_contrib` (combinatorial Laplacian), `degree`, `clustering_coeff`, `component_id`, `component_size`, `subgraph_slug` (nullable — null for country-level/small territories). Written by `FactorNodeWriter.write_spectral_nodes()` in Step 5c. Unique on `(snapshot_id, country_code, subgraph_slug, osm_id)`. Composite index on `(snapshot_id, country_code, subgraph_slug)` for subgraph-scoped pgvector `<#>` queries.
- **`factor_drift_node_metric`** — One row per `(country_code, snapshot_from_id, snapshot_to_id, osm_id)`. Fields: `fiedler_delta` (sign-aligned), `loading_drift` (cosine distance), `community_changed`, `degree_delta`. Written by `FactorNodeWriter.write_drift_nodes()` in Step 5d. Unique on `(country_code, snapshot_from_id, snapshot_to_id, osm_id)`.
- **`factor_amenity_embedding`** — One row per amenity vocab term. Fields: `amenity_text` (VarChar 200, unique), `embedding` (VectorField 300D, L2-normalized FastText). 621 rows. Written by `compute_amenity_embeddings` (an `init_planet` step).
- **`factor_subgraph_transport`** — One row per `(snapshot_id, country_code, subgraph_from, subgraph_to)`. Fields: `transport_matrix` (JSONField, k×k functional map), `k_dim`, `shared_entity_count`, `fit_residual`, `commutativity_residual`. Written by Step 5c Phase B (`SubgraphTransportService`). Used at runtime by `FactorResolutionService.diffusion_rank` for cross-subgraph spectral transport. See `docs/plans/completed/SUBGRAPH_FUNCTIONAL_MAPS_PLAN_v2.md`.

**Feature flags** (`settings.py`):
- `FACTOR_NODE_TABLES_ENABLED` (default `True`) — table path authoritative for `EVENT-DIFFUSION`, `PLACE-ATTRIBUTE-QUERY`, `COMMUNITY-DETECT`. Set to `false` only for debugging.

**Migrations**: `0012` (creates tables + `SeparateDatabaseAndState` for the `0011` GIN index), `0013` (`amenity_text` VarChar 100→200), `0014` (help_text sync).

**Test DB extensions**: Both `test_vector_db` and `test_django_db` need `vector` + `postgis` extensions (see `.devin/rules.md` §5.2); `test_vector_db` additionally needs `pg_trgm` + `fuzzystrmatch` for the QueryCorrectionService suite (`tests/unit/test_query_correction_service.py`).

### MCP Agent & Platform LLM layer

- `core/services/llm_service.py` — Provider-abstracted LLM client over
  `requests` (no SDK — backend is Python 3.8). Default `LLM_API_STYLE=native`
  uses Ollama `/api/chat` with `think: false` (qwen3 thinking mode otherwise
  eats the token budget and returns empty content on `/v1`); `openai` style
  for cloud swap. Fail-soft everywhere (`None`/`False`). Methods:
  `is_available` (cached), `chat`, `chat_json` (`format: "json"` on native),
  `chat_stream` (token deltas), `embed`. Config: `LLM_ENABLED`,
  `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_STYLE`, `LLM_TIMEOUT` (60 — cold
  reloads 5–15s). **Research instance (2026-09-18)**: `get_research_instance()`
  reads `RESEARCH_LLM_*` (batch orchestrator on the 2070; unset → platform
  instance). **Trace spans (2026-09-19)**: every call emits a
  `TraceService` span (model/temperature/tokens/latency) attached to the
  thread-local run trace. See `docs/Schematics/08_Agent_MCP_LLM/02_Platform_LLM_Enrichment.md`.
- `core/services/trace_service.py` — Unified LLM/run trace adapter
  (`docs/plans/completed/UNIFIED_LLM_TRACE_PLAN.md`). One schema
  (`trace` + `span` events), pluggable sinks: none (default) | console |
  file | langfuse (self-hosted, compose profile `observability`, SDK-free
  HTTP ingestion — backend is Python 3.8) | memory (tests). Bounded queue
  + daemon flush thread (batched by `TRACE_FLUSH_INTERVAL`, default 2s;
  drop-oldest on overflow), `TRACE_SAMPLE_RATE` roll per run, never
  raises, disabled = zero code path. API: `run_trace` (thread-local
  trace_id), `begin_span`/`end_span`/`span`, `point_span`, `stage_event`
  (deterministic question/tool markers behind
  `TRACE_DETERMINISTIC_STAGES=1`). Suite: `test_trace_service.py`.
- `semantic_search/services/research_service.py` — Research orchestrator
  (`ResearchOrchestratorService`): decompose (5-template manifest, max 6)
  → deterministic execute (`skip_enrichment=True`, anchor-qualifier retry,
  "the top <class>" placeholder substitution) → opt-in follow-up tools →
  grounded assemble. KE interviewer chat + structured brief finalize
  (chat_json extraction + deterministic render, `source:
  structured|fallback`). **Small-radius empty escalation (2026-09-19)**:
  a #1 question with a radius below `DEFAULT_NEAR_RADIUS_M` (2km) that
  returns zero results is widened to 2km and re-run once
  (`radius_escalated: true`); the decompose prompt scales radii to the
  anchor (city ≥ 2km, never 1km for a city). Demo:
  `research_demo` command. See `.devin/rules/13-research-orchestrator.md`.
- `semantic_search/services/query_enrichment_service.py` — LLM-driven answer
  enrichment (`synthesize()`): one LLM call grounded in pre-fetched entity
  context (USLP links + communities + class distribution), no tool
  selection. TTL answer cache (`LLM_ANSWER_CACHE_TTL_SECONDS`). Tool helpers
  (`_validate_tool_decision` / `_call_search_tool` / `_primary_digest`) are
  shared with the research orchestrator's follow-up tools. The old
  multi-tool `enrich()` loop was deleted 2026-09-26. Fail-soft.
- `worldkg_nca/snapshot_utils.py` — `get_latest_snapshot_id()` is the hot
  path (~850–980ms Append over every partition's pkey, called 3–4× per
  request); now TTL-cached (`SNAPSHOT_ID_CACHE_TTL_SECONDS`, default 120s,
  `clear_snapshot_cache()` after backfill). Executor chain: 2.6s → 61ms.
- `worldkg_nca/views/search.py` — `execute_query_stream` (plain Django view,
  `@require_GET` — DRF rejects `Accept: text/event-stream`) streams SSE:
  `parsed` → `executed` → `answer` → `context` → `answer_delta` → `done`
  (the `research`/`research_out` events were removed with `enrich()`,
  2026-09-26), pipeline in a worker thread + queue. `research_stream` /
  `research_chat` (AI SDK v7 UI-message-stream) / `research_finalize`
  (structured brief). **Traceability (2026-09-19)**: the three stream
  views wrap the run in `TraceService.run_trace` (thread-local), accept a
  cross-app `?trace_id=` / body `trace_id`, surface it in the `done`
  payload and the `X-Trace-Id` response header, and feed progress events
  through `stage_event` (behind `TRACE_DETERMINISTIC_STAGES=1`).
  `factor_availability` — G4 coverage booleans per table (spectral, drift
  via `snapshot_to_id` — DriftNodeMetric has no `snapshot_id` column,
  amenity embeddings, entity embeddings). Both exported in
  `views/__init__.py`.
- `core/management/commands/warm_llm.py` — loads the model at container
  create (entrypoint hook; `docker compose restart` does NOT re-run the
  entrypoint — use `up -d --force-recreate`).
- Frontend MCP: `frontend-v3/src/mcp/` — 10 tools at `/__mcp`. Data tools
  (`structuredSearch`/`nameSearch`/`templateQuery`/`getFactorAvailability`)
  run Node-side in `server-tools.ts` (headless-capable); app tools
  (`renderToolOverlay`, HITL, devtools) run browser-side in
  `client-functions.ts` (the Vue app only exists in the browser).
  `overlayStore.js` + `WorldKGMap.vue` render agent overlays. The SSE stream
  (`/api/nca/execute-query/stream/`) is consumed with `EventSource` (URL built
  from `axios.defaults.baseURL`). The embedded agent page (`AgentPlayground.vue`
  at `/agent`) and its `useTemplateQueryStream`/`agentQueryStore` were REMOVED
  2026-08-26 — the app is human-toolset-only; agent tool calls go through the
  MCP bridge (`mcp-smoke-test.mjs`).
- Docker: `ollama` (GPU-pinned to the 4070 Ti Super — `NVIDIA_VISIBLE_DEVICES=1`,
  `KEEP_ALIVE=-1`, `NUM_PARALLEL=6`) + `goose` (on-demand, `restart: "no"`,
  host networking, config dir mounted as a directory — goose rewrites
  `config.yaml` on session start and file bind-mounts fail with EBUSY).
  Pipeline worker pinned to the RTX 2070 (`NVIDIA_VISIBLE_DEVICES=0`).
  **NOTE 2026-09-11 (resolved 2026-09-16)**: `docker-compose.yml` sets the
  worker to `NVIDIA_VISIBLE_DEVICES=1` (the 4070 Ti Super, same card as
  ollama) — the documented 2070 pinning was never applied. BZ Step 5
  `train_gv_nle` OOM'd against ollama's resident qwen3:14b (≈10.2 GiB). The
  2026-09-16 switch to qwen3:8b (6.3 GiB resident, `ollama ps`) resolves it:
  ≈ 6.3 + 5.6 GiB peak pipeline compute < 16 GB, ~4 GB headroom — no more
  `docker compose down ollama` during normal pipeline runs.
- Tests: `test_llm_service.py`, `test_query_enrichment_service.py`,
  `test_snapshot_cache.py` — all LLM/DB-mocked, hermetic.
- See `docs/Schematics/08_Agent_MCP_LLM/` (4 schematics: tool surface,
  platform LLM, SSE/performance, Docker agent stack).

### MapQA self-supervised training data
(`docs/plans/completed/MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md` — Part 1 of 2)

- `semantic_search/services/mapqa_question_generator.py` —
  `MapQAQuestionGenerator` generates question–answer pairs for the 5 existing
  macro-templates (#1, #2, #4, #5, #8) from `OsmEntity` ground truth in the
  vectors DB. Answers are computed exactly from PostGIS at generation time
  (`ST_DWithin` counts, haversine, nearest-entity ordering, bearing →
  cardinal). Deterministic: pool selection uses `md5(concat(osm_type, ':',
  osm_id::text))` ordering + a seeded `random.Random` — same seed + snapshot →
  identical rows. Anchor-relative samplers (amenity chosen from the pool
  within the anchor's radius/cone) guarantee non-empty answers by
  construction. Never emits empty answers (raw-dataset bug regression).
- `semantic_search/services/mapqa_paraphraser.py` — rule-based tier (always
  on; synonym maps + voice transforms, slot-invariant) + optional LLM tier
  (Ollama via `LLMService`, gated by `MAPQA_LLM_AUGMENTATION_ENABLED`,
  default false). Every candidate — both tiers — is validated to contain all
  slot values verbatim; slot drift is rejected (breaks ground truth).
- `generate_mapqa_training_data` management command:
  ```bash
  python manage.py generate_mapqa_training_data --country BZ,JM,IE --per-class 250
  python manage.py generate_mapqa_training_data --all-processed --dry-run
  python manage.py generate_mapqa_training_data --all-processed --retrain
  ```
  Appends to `{MAPQA_PARSER_DATA_DIR}/training_data/natural_language_qa_pairs.csv`
  (source-controlled: `backend/data/mapqa_parser/training_data/`).
  Idempotent: prior `Source=self_supervised` rows for the same
  (Country_code, Snapshot_date, Macro-template) are replaced, never
  duplicated; the CSV is rewritten atomically (temp file + rename).
  Stratified 20% per-template holdout → `Region=self_supervised_test`
  (per-class metrics in `metrics.json`; Illinois stays the headline
  zero-shot metric to avoid train/test leakage). Class-balance guard:
  `--max-per-class` (default 1500) caps per-template totals, seeds first.
- `train_mapqa_parser` train split now includes `Region=self_supervised`
  (train = california_full + augmented + self_supervised; test =
  illinois_test; holdout = self_supervised_test). The loader tolerates both
  CSV schemas (7-column legacy rows get defaults: Source derived from Region
  — mapqa-llm / hand-curated / self_supervised). `metrics.json` gains
  `self_supervised_samples` + `self_supervised_holdout`.
- Tests: `tests/unit/test_mapqa_question_generator.py` (ground-truth
  recomputations, determinism, idempotency, sanitization, train-split),
  `tests/unit/test_mapqa_paraphraser.py` (slot invariance, LLM gate),
  `tests/integration/test_mapqa_executor.py::TestSelfSupervisedQuestionsExecute`
  (endpoint 200s for generated questions).

### Test-fixture data-loss hazard (BZ restore note)

Module-scoped fixtures that write to the DB MUST depend on
`django_db_setup` (e.g. `def seed(django_db_setup, django_db_blocker)` +
`with django_db_blocker.unblock():`). Without it, pytest runs module-scoped
fixtures BEFORE test-DB setup redirects the connections, so writes hit the
REAL databases. This happened on 2026-08-25: a generator test fixture
deleted all BZ rows from the real vectors DB and recreated the BZ
`CountryPipelineProfile`. Recovery: recreated the profile from the original
run's `configuration` JSON (temp script, since removed) and re-ran
`python manage.py run_pipeline BZ --wait` (eager, uses the existing
temporal snapshot PBF at
`osm_wikidata_extractions/central_america/belize/temporal_snapshots/`).
The eager path skips the Step 5b subgraph chord, so GV-NLE for subgraph
entities was restored separately via `GvNleTrainingService().run_subgraph`
per subgraph (pickles pre-existed on disk). Check `pipeline_runs` status
for BZ before assuming the restore completed.

## Verification

```bash
# System check (Docker)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py check

# Migration consistency
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py makemigrations --check --dry-run

# Model import + app label verification (core + osmsnapshot)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py shell -c "
from core.models import PbfFile, PipelineRun, ProcessingSession, CountryPipelineProfile, OSMWikiDataHierarchy, PartitionRegistry
from osmsnapshot.models import Snapshot, SnapshotJob, WorldKGClassDrift, WorldKGClassFingerprint, PbfTagDistribution
from api.models import PbfFile as P2, Snapshot as S2, SnapshotJob as SJ2
assert PbfFile._meta.app_label == 'core'
assert PipelineRun._meta.app_label == 'core'
assert Snapshot._meta.app_label == 'osmsnapshot'
assert SnapshotJob._meta.app_label == 'osmsnapshot'
assert P2 is PbfFile
assert S2 is Snapshot
assert SJ2 is SnapshotJob
assert WorldKGClassDrift._meta.get_field('snapshot_from').remote_field.model is Snapshot
print('OK')
"

# Stale import check (should return nothing)
grep -r "from extraction\|import extraction\|from orchestration\|import orchestration" backend/ --include="*.py" | grep -v "/migrations/"
grep -r "from analysis\|import analysis" backend/ --include="*.py" | grep -v "/migrations/"
grep -r "TemporalSnapshot\|AssetBundle" backend/ --include="*.py" | grep -v "/migrations/"

# init_planet command is registered
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py init_planet --help

# Worker starts cleanly with only Steps 1-6 + 5c/5d registered (no planet init tasks)
docker compose -f docker-compose.yml -f compose.override.yml logs worker --since 30s | grep -E "step_[0-9]" | grep -v "step_1\|step_2\|step_3\|step_4\|step_5\|step_5c\|step_5d\|step_6"

# Syntax check all modified files
cd backend && python -c "
import ast, glob
for f in glob.glob('**/*.py', recursive=True):
    if '/migrations/' in f: continue
    with open(f) as fh: ast.parse(fh.read())
print('OK')
"

# Factor-node table verification (vectors DB)
docker compose -f docker-compose.yml -f compose.override.yml exec postgres-vectors \
  psql -U vector_user -d vector_db -c "
SELECT 'spectral' AS tbl, count(*) FROM factor_spectral_node_metric
UNION ALL SELECT 'drift', count(*) FROM factor_drift_node_metric
UNION ALL SELECT 'amenity', count(*) FROM factor_amenity_embedding;
"

# Factor-node + MapQA regression (88 tests)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python -m pytest \
  tests/unit/test_mapqa_parser.py \
  tests/integration/test_mapqa_executor.py \
  tests/unit/test_spectral_analysis_service.py \
  tests/unit/test_graph_signal_service.py \
  tests/unit/test_community_detection_service.py \
  tests/unit/test_spectral_drift_service.py \
  tests/unit/test_factor_node_tables.py \
  -v --reuse-db

# Romanizer unit tests (45 tests, no DB needed)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python -m pytest \
  tests/unit/test_romanizers.py -v

# Romanize names (cross-script name matching — run after migrate + init_planet)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py romanize_names
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py romanize_names --country-code KR --dry-run

# Verify romanized names in the vectors DB
docker compose -f docker-compose.yml -f compose.override.yml exec postgres-vectors \
  psql -U vector_user -d vector_db -c "
SELECT country_code, COUNT(*) FILTER (WHERE name_romanized IS NOT NULL) AS romanized,
       COUNT(*) FILTER (WHERE name_romanized IS NULL AND tags ? 'name') AS pending
FROM semantic_search_osmentity WHERE tags ? 'name'
GROUP BY country_code ORDER BY romanized DESC LIMIT 10;"
```

## Self-hosted deployment (2026-09-13)

The frontend is self-hosted, not on Netlify. See
`docs/plans/completed/SELF_HOSTED_DEPLOYMENT_PLAN.md` and the superseded
`docs/plans/next-stage/NETLIFY_FRONTEND_DEPLOYMENT_PLAN.md`.

- `docker-compose.yml` has an `nginx` service (eda-nginx, :80) serving
  `frontend-v3/dist` statically (SPA fallback) and proxying `/api` (REST +
  SSE, buffering off) and `/ws` (WebSocket upgrade) to `backend:8000`.
- The Tailscale Funnel targets :80 (nginx), not :8000. Public URL:
  `https://thanos.tail560528.ts.net`. SPA build bakes
  `VITE_API_BASE_URL=/api` (same-origin; no CORS).
- Login guard: `backend/middleware.py` `PublicAuthGuardMiddleware` 401s
  unauthenticated `/api/*` on public hosts (open: `/api/auth/*`,
  `/api/system/status/`); `AdminHostGateMiddleware` 404s `/admin*`
  publicly. Session auth endpoints in `api/views_auth.py` (`/api/auth/`).
  Registration gated by `SIGNUP_INVITE_CODE` env var. WS consumer closes
  public anonymous connections (4401). Frontend: LoginView.vue,
  `stores/authStore.js`, router requiresAuth guard, axios CSRF/401
  interceptors in main.js.
- Ops: rebuild SPA with `VITE_API_BASE_URL=/api npm run build` in
  `frontend-v3/`; `.env` changes need `docker compose up -d --force-recreate
  backend`; local/trusted hosts configurable via `TRUSTED_LOCAL_HOSTS`.
