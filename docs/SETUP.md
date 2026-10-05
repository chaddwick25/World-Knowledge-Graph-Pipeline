# Setup

Environment setup for the WorldKG Pipeline. Covers Docker Compose,
database extensions, migrations, planet initialization, name romanization,
worker configuration, and fresh-database resets.

For architecture and operational details, see:
- `docs/Schematics/README.md` — 7 schematic categories with file:line refs
- `docs/Schematics/04_ETL_Django_Vue_Primitives/01_Docker_Entrypoint_Init.md` — `init_planet` architecture
- `.devin/rules.md` — development rules, conventions, gotchas

---

## Prerequisites

- Docker and Docker Compose
- NVIDIA GPU + NVIDIA Container Toolkit (for GV-NLE, IGEA, USLP — CPU fallback exists for some steps)
- ~70 GB free disk for the planet PBF and per-country extracts

---

## Docker Compose (recommended)

The project uses Docker Compose to orchestrate all services. This is the
recommended development and deployment method.

```bash
# Build and start all services
docker compose -f docker-compose.yml -f compose.override.yml up -d

# View logs
docker compose logs -f backend worker

# Stop services
docker compose down
```

The frontend dev server can run locally and proxy `/api` to the Docker
backend at `http://localhost:8000`:

```bash
cd frontend-v3
npm install
npm run dev        # Dev server on http://localhost:5173
npm run build      # Production build
```

---

## Database extensions (one-time)

Both databases need their extensions enabled after the first `up`:

```bash
# default DB (port 5432) — PostGIS + pgvector
docker compose exec postgres-default bash -c '
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -h localhost -p 5432 \
       -c "CREATE EXTENSION IF NOT EXISTS postgis;";
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -h localhost -p 5432 \
       -c "CREATE EXTENSION IF NOT EXISTS vector;"
'

# vectors DB (port 5433) — pgvector + PostGIS
docker compose exec postgres-vectors bash -c 'psql -U "$PGVECTOR_USER" -d "$PGVECTOR_DB" -c "CREATE EXTENSION IF NOT EXISTS vector;"'
```

The vectors database must have both `pgvector` and `PostGIS` extensions
(configured via `postgres-vectors.Dockerfile`).

---

## Migrations

```bash
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py migrate --database=vectors
```

The backend container runs migrations automatically on startup when
`RUN_MIGRATIONS=true` (default). The worker container polls
`django_migrations` and waits for `core.0001_initial` to appear before
starting.

---

## Planet initialization (one-time, idempotent)

A single `init_planet` management command runs once as a Docker entrypoint
step after migrations. It is idempotent (gated by a `PlanetSnapshot` soft
lock) and initializes: planet/continent PBFs, OSM-Wikidata hierarchy,
country paths, subgraph profiles, Wikidata IDs, embedding scans, WorldKG
ontology, and OSM boundaries.
```bash
# Run all 16 steps (idempotent — short-circuits if a COMPLETED row exists for today)
docker compose exec backend python manage.py init_planet

# Run a single step
docker compose exec backend python manage.py init_planet --step <name>

# Force re-run (bypass the soft lock)
docker compose exec backend python manage.py init_planet --no-lock
```

The backend container runs `init_planet` automatically on startup when
`RUN_INIT_PLANET=true` (default) — but only if planet init is not already
complete: the entrypoint pre-checks `planet_snapshots` for a `COMPLETED`
row (a cheap `SELECT 1 ... LIMIT 1`, mirroring the worker migration poll)
and skips the command entirely when one exists. The worker has
`RUN_INIT_PLANET=false`.

For the 16-step list and architecture, see
`docs/Schematics/04_ETL_Django_Vue_Primitives/01_Docker_Entrypoint_Init.md`.

## Path verification (boot-time)

`verify_paths` validates the configured path table, DB connectivity, and
(since 2026-09-11) every file referenced by `overrides.json` — the override
file itself, each `embedding_splits` poly (resolved against
`POLYGON_FILES_DIR/overrides/` then root), and split output parent dirs.
Use `--strict` to exit 1 on missing required paths or polys:

```bash
docker compose exec backend python manage.py verify_paths [--strict]
```

Deployments without an `overrides.json` skip the overrides section cleanly
(a single optional warning at most).

---

## Overrides (cold-storage configuration)

`overrides.json` carries country slug mappings, embedding split/merge
targets, shared-embedding edge cases, and optional continent overrides. It
lives in cold storage and is located via the `OVERRIDES_JSON_PATH` env var
(default `COLD_STORAGE_BASE_DIR/overrides.json`, see
`backend/backend/settings.py:155`). Deployments without one skip cleanly.

A schema-complete, commented example ships at `overrides.example.json` in
the repo root. Each of the four sections (`country_iso_overrides`,
`embedding_edge_cases`, `embedding_splits`, `continents`) carries a `_docs`
key explaining its fields and the loader entry point; the rest of each
section shows one realistic worked example. JSON has no comments, so all
explanation lives in those `_docs` keys.

To bootstrap a deployment:

```bash
# 1. Copy the example to your cold-storage path and edit values
cp overrides.example.json /media/.../OSM/overrides.json
# then edit: country slugs, split polys (point at real .poly files in
# POLYGON_FILES_DIR), merge shards, edge cases.

# 2. Point the env var at it (if not using the default location)
export OVERRIDES_JSON_PATH=/media/.../OSM/overrides.json

# 3. Validate at boot
docker compose exec backend python manage.py verify_paths --strict
```

`verify_paths` resolves every `embedding_splits.splits[].targets[].poly`
against `POLYGON_FILES_DIR/overrides/` then root (the same order as
`EmbeddingSpatialSplitService.resolve_poly_path`), so a broken overrides
config is caught at boot, not mid-pipeline. Polys live in cold storage, not
the repo, so a bare checkout will report them as missing until you
provision the real `.poly` files — that is correct behavior.

Canonical field reference:
`docs/preserving_the_process/NON_SOVEREIGN_TERRITORIES_AND_OVERRIDES.md`
sections 2.1–2.3. Loader:
`backend/core/services/snapshot/country_override_service.py:load_overrides()`
(also accepts a flat legacy mapping like `{"IE": "ireland"}` at the top
level, but the structured schema in the example is preferred).

---

## Romanize entity names (one-time per country)

The `romanizing_names` service converts non-Latin and diacritic-bearing OSM
names to a Latin phonetic fingerprint for PostgreSQL trigram similarity
search. It runs as a standalone management command, decoupled from country
ingestion — idempotent and resumable.

```bash
# Romanize all pending entities (processes where name_romanized IS NULL)
docker compose exec backend python manage.py romanize_names

# Romanize a specific country
docker compose exec backend python manage.py romanize_names --country-code KR

# Dry run (no DB writes)
docker compose exec backend python manage.py romanize_names --country-code KR --dry-run
```

See `docs/Schematics/07_Romanizer/01_Deterministic_Cross_Script_Romanization.md`
for the full architecture.

---

## Worker configuration

- `--pool=prefork --concurrency=4` — 4 CPU workers for parallel upserts/IGEA/USLP
- GPU tasks use per-GPU slot locks — all subgraphs go to `cuda:0` with
  concurrency=1 by default. Configure via `GV_NLE_GPU_DEVICES` and
  `GV_NLE_GPU_CONCURRENCY` env vars.
- `RUN_MIGRATIONS` env var: backend runs migrations, worker waits for them
- `RUN_INIT_PLANET` env var: backend runs `init_planet` after migrations,
  worker skips it

---

## Fresh database reset

After dropping and recreating both databases:

```bash
# 1. Migrate both databases
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py migrate
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py migrate --database=vectors

# 2. Initialize planet data (hierarchy, profiles, paths, embeddings, ontology, boundaries)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py init_planet

# 3. Romanize entity names (cross-script name matching)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py romanize_names

# 4. Amenity → WorldKG class mappings (rule 6.2 lookup table)
docker compose -f docker-compose.yml -f compose.override.yml exec backend python manage.py compute_amenity_class_mappings
```

The backend container's entrypoint runs `init_planet` automatically on
startup (`RUN_INIT_PLANET=true`), so simply restarting the backend after a
DB reset will trigger it. The worker has `RUN_INIT_PLANET=false`.
`init_planet` also rebuilds the WorldKG ontology cache, the parser model,
and the amenity embeddings; the two commands above are the manual
rebuilds (both deliberately decoupled from `init_planet`).

The romanizer's jamo mappings live in source code
(`semantic_search/services/romanizing_names/hangul_romanizer.py`,
git-tracked), not in any database. A reset wipes the `name_romanized`
column but never the mapping; step 3 rebuilds it deterministically.

On a fresh vectors DB, `semantic_search_osmentity` starts as a monolith
table. The first country pipeline run's Step 1 detects the empty monolith,
converts it to a partitioned table (`PARTITION BY LIST (snapshot_id)` →
`LIST (country_code)`), and creates the per-country leaf partition
automatically — no manual cutover needed.
