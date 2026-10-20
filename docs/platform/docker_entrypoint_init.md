# Docker Entrypoint Init: `init_planet` as a Batch Preprocessing Step

> **Focus:** how planet initialization works as a Docker entrypoint step
> (the `init_planet` management command), the 16 steps it runs, the
> `PlanetSnapshot` soft lock, and the distinction between entry-script
> initialization and batch processing in the country pipeline.
>
> **Key idea:** planet init is **not** a Celery canvas. It is a synchronous
> management command run as a Docker entrypoint step after migrations,
> gated by the `RUN_INIT_PLANET` env var. It is idempotent via the
> `PlanetSnapshot` soft lock, and the entrypoint pre-checks `planet_snapshots`
> for a `COMPLETED` row so an already-initialized deployment skips the
> command entirely on restart. This replaces the former Celery canvas of
> `step_0a`-`step_0m` tasks (which have been deleted).

---

## 1. The Docker Entrypoint Script

**Source:** `docker-entrypoint.sh` (in the backend container)

```bash
# 1. Run migrations (backend container only, gated by RUN_MIGRATIONS)
if [ "$RUN_MIGRATIONS" = "true" ]; then
    python manage.py migrate
    python manage.py migrate --database=vectors
fi

# 2. Worker waits for migrations to complete (polls django_migrations)
if [ "$RUN_MIGRATIONS" = "false" ]; then
    # Poll until core.0001_initial appears in django_migrations
    until python -c "..." 2>/dev/null; do sleep 2; done
fi

# 3. Run init_planet (backend container only, gated by RUN_INIT_PLANET,
#    then by a cheap COMPLETED PlanetSnapshot pre-check — added 2026-09-10)
if [ "$RUN_INIT_PLANET" != "false" ]; then
    PLANET_COMPLETE=$(psql ... -tAc \
      "SELECT 1 FROM planet_snapshots WHERE status = 'COMPLETED' LIMIT 1;")
    if [ -n "$PLANET_COMPLETE" ]; then
        echo "Planet init already complete; skipping init_planet."
    else
        python manage.py init_planet || echo "WARNING: init_planet reported failures..."
    fi
fi

# 4. Start the actual service (gunicorn for backend, celery for worker)
exec "$@"
```

### 1.1 Why a Docker Entrypoint Step (Not Celery)

The former Celery canvas (`step_0_initialize_planet` → `step_0b` → ... →
`step_0m` → `_finalize_planet_init_chain`) had several problems:
- **Startup ordering.** The country pipeline depends on planet init being
  complete. A Celery canvas is async, so the API server would start before
  init finished.
- **Idempotency.** The Celery canvas had no built-in idempotency guard.
  Restarting the worker would re-run the whole canvas.
- **Failure handling.** A single failed step would leave the planet in a
  partial state with no easy way to resume.

The `init_planet` management command solves all three:
- **Synchronous**: runs before the API server starts (gated by
  `RUN_INIT_PLANET=true` on the backend container).
- **Idempotent**: uses `PlanetSnapshot` as a soft lock, skips if a
  COMPLETED row exists for today's date. On top of that, the entrypoint now
  pre-checks `planet_snapshots` for any `COMPLETED` row and skips invoking
  the command entirely on already-initialized deployments (2026-09-10).
- **Resumable**: `--step <name>` runs a single step; `--no-lock` forces
  re-run.

### 1.2 Best-Effort Execution

`init_planet` failures do **not** abort container startup. The entrypoint
script uses `|| echo "WARNING: ..."` so the container still starts. This
matches the old Celery `max_retries=1` behavior: a transient failure (e.g.
Wikidata SPARQL timeout) should not block the API server from starting.

---

## 2. The `init_planet` Management Command

**Source:** <ref_file file="backend/core/management/commands/init_planet.py" />

### 2.1 The 16 Steps

| # | Step Name | Purpose |
|---|---|---|
| 1 | `register_planet` | Registers planet PBF + Wikidata hierarchy skeleton via `PlanetInitializationService` |
| 2 | `extract_continents` | Extracts continent PBFs from planet file via osmium |
| 3 | `sync_hierarchy` | Re-syncs `country_relations.json`, imports, pre-builds profiles |
| 4 | `prebuild_country_paths` | Resolves TSV/PBF/pickle paths on `CountryPipelineProfile` |
| 5 | `prebuild_subgraphs` | Generates `SubgraphProfile` rows from Geofabrik hierarchy |
| 6 | `prebuild_wikidata_ids` | Backfills Q-IDs on `CountryPipelineProfile`/`SubgraphProfile` |
| 7 | `copy_gb_to_uk` | Copies great-britain TSVs to united-kingdom naming |
| 8 | `scan_embeddings` | Scans `EMBEDDINGS_ROOT`, populates `EligibleCountry` rows |
| 9 | `split_embeddings` | Splits multi-country TSVs (GB, MY/SG/BN) into per-country TSVs |
| 10 | `merge_us_embeddings` | Merges 5 US regional shards into single US TSVs |
| 11 | `rescan_embeddings` | Re-scans after split/merge so `EligibleCountry` reflects READY |
| 12 | `enrich_worldkg_classes` | Loads WorldKG ontology TTL into Redis for fast lookup |
| 13 | `generate_osm_boundaries` | Generates OSM administrative boundaries for cartography |
| 14 | `train_mapqa_parser` | Trains the MapQA template classifier from training CSVs |
| 15 | `compute_amenity_embeddings` | Precomputes FastText embeddings for the MapQA amenity vocab |
| 16 | `finalize` | Marks `PlanetSnapshot` row as COMPLETED |

### 2.2 The PlanetSnapshot Soft Lock

1. `init_planet` starts.
2. Check: `PlanetSnapshot.objects.filter(snapshot_date=today,
   status='COMPLETED').exists()`?
   - YES and not `--no-lock` → log "already initialized", exit 0
   - NO or `--no-lock` → continue
3. Create `PlanetSnapshot(status='RUNNING', snapshot_date=today)`.
4. Run 16 steps (or `--step <name>` for a single step).
5. All steps succeed → `PlanetSnapshot.status = 'COMPLETED'`.
   Any step fails → `PlanetSnapshot.status = 'FAILED'` (best-effort).

### 2.3 CLI Flags

| Flag | Effect |
|---|---|
| `--step <name>` | Run only the named step from the 16-step list |
| `--no-lock` | Skip the `PlanetSnapshot` run-lock check (use with care) |
| `--skip-continents` | Skip continent PBF extraction (only safe if already done) |
| `--skip-embeddings` | Skip embedding scan/split/merge steps |

### 2.4 Manual Invocation

```bash
# Full run (idempotent — skips if already COMPLETED today)
python manage.py init_planet

# Force re-run (ignores soft lock)
python manage.py init_planet --no-lock

# Single step
python manage.py init_planet --step generate_osm_boundaries

# Skip continent extraction (already done)
python manage.py init_planet --skip-continents
```

---

## 3. Preprocessing vs Batch Processing

The pipeline distinguishes two kinds of work.

### 3.1 Preprocessing (init_planet, entry script)

- **Runs once** at container startup (or manually)
- **Idempotent**, safe to re-run
- **No per-country work**, sets up global config, primitives, ontology
- **Synchronous**, blocks container startup until done (or fails
  best-effort)
- **Output**: DB rows (`CountryPipelineProfile`, `SubgraphProfile`,
  `EligibleCountry`, `OsmBoundary`), Redis cache (ontology), filesystem
  artifacts (continent PBFs, split TSVs)

### 3.2 Batch Processing (country pipeline, Celery canvas)

- **Runs per country** on demand (via API or manual trigger)
- **Not idempotent**, `force=true` required to re-run a COMPLETED job
- **Per-country work**, Steps 1-6 (embed, enrich, align, predict, train,
  mark ready)
- **Asynchronous**, Celery canvas dispatched via `apply_async`
- **Output**: `OsmEntity` rows (vectors DB), `SpatialTripletScore` rows,
  `SnapshotJob` status, GV-NLE embeddings, MV + HNSW index (Step 6)

| | Preprocessing (`init_planet`) | Batch processing (Celery) |
|---|---|---|
| When | container startup | API trigger per country |
| How | management command | Celery canvas (chain) |
| Lock | `PlanetSnapshot` soft lock | `SnapshotJob` (409 if RUNNING) |
| Scope | global (all countries) | one country |
| Steps | 16 init steps | 6 pipeline steps |
| Output | config + primitives | embeddings + links |
| Idempotent | YES | NO (`force=true`) |

---

## 4. What init_planet Produces

### 4.1 Database Rows (default DB)

| Model | Step | Purpose |
|---|---|---|
| `PlanetSnapshot` | 1, 16 | Soft lock for init_planet idempotency |
| `CountryPipelineProfile` | 3, 4 | Country config (ISO, slug, Wikidata URI, Geofabrik URL, GeoVectors TSV paths, bbox) |
| `SubgraphProfile` | 5, 6 | Subdivision config (slug, Wikidata QID, admin_level, bbox, poly/pbf/pickle paths) |
| `EligibleCountry` | 8, 11 | Which countries have GeoVectors embeddings (READY/PENDING) |
| `OsmBoundary` | 13 | High-precision bbox from continent recipes |

### 4.2 Redis Cache

| Key Pattern | Step | Purpose |
|---|---|---|
| `worldkg:class:*` | 12 | WorldKG ontology classes (loaded from TTL) |
| `worldkg:wikidata_to_wkg` | 12 | Wikidata QID → WorldKG class map |

### 4.3 Filesystem Artifacts

| Path | Step | Purpose |
|---|---|---|
| `data/continents/*.pbf` | 2 | Continent PBFs (extracted from planet) |
| `data/embeddings/{country}/*.tsv` | 9, 10 | Per-country GeoVectors TSVs (split/merged) |

---

## 5. The Migration Race Fix

The backend and worker containers both run `docker-entrypoint.sh`
concurrently. Without coordination, they race on `CREATE TYPE` in
migrations (`pg_type_typname_nsp_index` UniqueViolation).

**Fix:** the `RUN_MIGRATIONS` env var:
- Backend (`RUN_MIGRATIONS=true`): runs migrations
- Worker (`RUN_MIGRATIONS=false`): polls the `django_migrations` table for
  `core.0001_initial` until the backend finishes, then starts Celery

This prevents the concurrent `CREATE TYPE` race without a separate
migration step.

---

## 6. Invariants

- Planet init is NOT a Celery canvas. It is the `init_planet` management
  command.
- `run_planet_initialization()` / `run_continent_initialization()` in
  `canvas.py` are thin facades that delegate to the command synchronously.
- The `step_0*` tasks have been deleted. Do not reference them.
- `init_planet` is idempotent via the `PlanetSnapshot` soft lock.
- `init_planet` failures are best-effort. They do not block container
  startup.
- The backend container runs `init_planet` (`RUN_INIT_PLANET=true`); the
  worker container skips it (`RUN_INIT_PLANET=false`). The backend skips
  the command when a `COMPLETED` row already exists in `planet_snapshots`
  (entrypoint pre-check, `SELECT 1 ... LIMIT 1`).
