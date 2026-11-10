#!/bin/bash
# Fresh planet build: stage a tree, create the database pair, run planet init.
#
# This exists so a build is one command instead of a runbook, and so the steps can be lifted into Ansible for cloud deployment
# (task-per-step) and Terraform (the host/DB/DNS edges) later.
#
#   scripts/fresh_planet_build.sh \
#     --planet /path/to/OSM-PBF-FILES/planet/history-YYMMDD.osh.pbf \
#     --tree   /path/to/worldkg_v3 \
#     --db-suffix v3
#
# Encoded learnings (each one cost a failed run):
#   * the poly directory must be populated BEFORE extract_continents, or the
#     hierarchy self-heal has nothing to import;
#   * the WorldKG ontology TTL is an input, not an artifact - enrich_worldkg_
#     classes fails without it;
#   * continent extracts must be named .osh.pbf so the header reports history;
#   * the snapshot date must not sit past the planet's ceiling, so it defaults
#     to the planet's own date rather than "today";
#   * the live database pair is never a valid target.
#
# Safe by default: prints the plan, refuses to clobber, and never touches the
# live pair. --dry-run shows everything without changing anything.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

PLANET=""; TREE=""; DB_SUFFIX=""; SNAPSHOT_DATE=""
DRY_RUN=0; ASSUME_YES=0; SKIP_DB=0; SKIP_BUILD=0; VERIFY_MD5=1; FORCE=0

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --planet PATH         history dump (.osm.pbf / .osh.pbf), required
  --tree PATH           new data tree, created if absent, required
  --db-suffix NAME      databases become django_db_NAME / vector_db_NAME
  --snapshot-date DATE  YYYY_MM_DD; default is the planet's own date
  --no-md5              skip checksum verification
  --skip-db             do not create or migrate databases
  --skip-build          stage everything, do not start the build
  --force               allow an existing tree with continents / existing DBs
  --dry-run             print the plan, change nothing
  --yes                 do not ask for confirmation

Remote database host: POSTGRES_HOST / PGVECTOR_HOST / REDIS_HOST default to the
compose service names. For a remote data tier export them first, e.g.
  POSTGRES_HOST=<db-host> PGVECTOR_HOST=<db-host> REDIS_HOST=<redis-host>

Name prefixes (DB_PREFIX / VECTOR_DB_PREFIX, default django_db / vector_db) and
the compose service (SERVICE, default backend) are env-overridable too.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --planet) PLANET="${2:-}"; shift 2 ;;
        --tree) TREE="${2:-}"; shift 2 ;;
        --db-suffix) DB_SUFFIX="${2:-}"; shift 2 ;;
        --snapshot-date) SNAPSHOT_DATE="${2:-}"; shift 2 ;;
        --no-md5) VERIFY_MD5=0; shift ;;
        --skip-db) SKIP_DB=1; shift ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo -e "${RED}Unknown option: $1${NC}"; usage; exit 2 ;;
    esac
done

say()  { echo -e "${BLUE}$*${NC}"; }
ok()   { echo -e "${GREEN}  ok  $*${NC}"; }
warn() { echo -e "${YELLOW}  warn $*${NC}"; }
die()  { echo -e "${RED}error: $*${NC}" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

# ── configuration ────────────────────────────────────────────────────────
if [ -f .env.local ]; then set -a; . ./.env.local; set +a
elif [ -f .env ]; then set -a; . ./.env; set +a; fi

POSTGRES_HOST="${POSTGRES_HOST:-postgres-default}"
PGVECTOR_HOST="${PGVECTOR_HOST:-postgres-vectors}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
PGVECTOR_PORT="${PGVECTOR_PORT:-5433}"
REDIS_HOST="${REDIS_HOST:-redis}"

# Nothing deployment-specific is hardcoded: the compose service, the in-container
# data path, and the live database names all come from the environment so this
# script is portable across hosts (and lifts cleanly into Ansible variables).
SERVICE="${SERVICE:-backend}"
CONTAINER_DATA_DIR="${CONTAINER_DATA_DIR:-/app/data}"
COMPOSE_CMD="${COMPOSE_CMD:-docker compose}"
COMPOSE=( ${COMPOSE_CMD} run --rm --no-deps )

# The live pair, as configured. Used only to refuse targeting it.
LIVE_POSTGRES_DB="${LIVE_POSTGRES_DB:-${POSTGRES_DB:-django_db}}"
LIVE_PGVECTOR_DB="${LIVE_PGVECTOR_DB:-${PGVECTOR_DB:-vector_db}}"

[ -n "$PLANET" ] || die "--planet is required"
[ -n "$TREE" ]   || die "--tree is required"

PLANET_DIR="$(cd "$(dirname "$PLANET")" && pwd)"
PLANET_BASE="$(basename "$PLANET")"
POLY_SOURCE="${POLY_SOURCE:-$PLANET_DIR/../osm_polygon_files}"
ONTOLOGY_SOURCE="${ONTOLOGY_SOURCE:-$PLANET_DIR/../world_kg_ontology}"

# The planet's own date, e.g. history-261102 -> 2026_11_02. Using "today"
# would put the snapshot past the data ceiling whenever the weekly dump lags.
derive_date() {
    if [[ "$PLANET_BASE" =~ ^history-([0-9]{2})([0-9]{2})([0-9]{2}) ]]; then
        echo "20${BASH_REMATCH[1]}_${BASH_REMATCH[2]}_${BASH_REMATCH[3]}"
    fi
}
[ -n "$SNAPSHOT_DATE" ] || SNAPSHOT_DATE="$(derive_date)"
[ -n "$SNAPSHOT_DATE" ] || die "cannot derive the snapshot date from '$PLANET_BASE'; pass --snapshot-date"

if [ -n "$DB_SUFFIX" ]; then
    DB_PREFIX="${DB_PREFIX:-django_db}"
    VECTOR_DB_PREFIX="${VECTOR_DB_PREFIX:-vector_db}"
    DEFAULT_DB="${DB_PREFIX}_${DB_SUFFIX}"; VECTORS_DB="${VECTOR_DB_PREFIX}_${DB_SUFFIX}"
else
    DEFAULT_DB="${POSTGRES_DB:-}"; VECTORS_DB="${PGVECTOR_DB:-}"
fi
[ -n "$DEFAULT_DB" ] || die "pass --db-suffix (or set POSTGRES_DB/PGVECTOR_DB)"

# Guard: the live pair is never a valid target.
for db in "$DEFAULT_DB" "$VECTORS_DB"; do
    if [ "$db" = "$LIVE_POSTGRES_DB" ] || [ "$db" = "$LIVE_PGVECTOR_DB" ]; then
        die "refusing to target the live database '$db' (set LIVE_POSTGRES_DB/LIVE_PGVECTOR_DB if this is wrong)"
    fi
done

# History files must end in .osh.pbf for osmium to read them as history.
# Accept any of the three shapes a download can arrive in, and do not
# double-suffix a name that is already correct.
OSH_PLANET="${PLANET%.crdownload}"
case "$OSH_PLANET" in
    *.osh.pbf) ;;
    *.osm.pbf) OSH_PLANET="${OSH_PLANET%.osm.pbf}.osh.pbf" ;;
    *)         OSH_PLANET="${OSH_PLANET}.osh.pbf" ;;
esac

say "=========================================================="
say " Fresh planet build"
say "=========================================================="
echo "  planet        $PLANET"
echo "  tree          $TREE"
echo "  snapshot      $SNAPSHOT_DATE"
echo "  databases     $DEFAULT_DB / $VECTORS_DB"
echo "  postgres      $POSTGRES_HOST:$POSTGRES_PORT  vectors $PGVECTOR_HOST:$PGVECTOR_PORT"
echo "  md5 verify    $([ "$VERIFY_MD5" = 1 ] && echo yes || echo no)"
[ "$DRY_RUN" = 1 ] && warn "DRY RUN - nothing will change"

if [ "$ASSUME_YES" = 0 ] && [ "$DRY_RUN" = 0 ]; then
    read -r -p "Proceed? (yes/no): " confirm
    [ "$confirm" = "yes" ] || [ "$confirm" = "y" ] || { echo "aborted"; exit 0; }
fi

# ── 1. planet ────────────────────────────────────────────────────────────
say "1. planet"
[ -f "$PLANET" ] || die "planet not found: $PLANET"
case "$PLANET_BASE" in
    *.crdownload) die "'$PLANET_BASE' still looks like an in-progress download" ;;
esac

if [ "$VERIFY_MD5" = 1 ]; then
    # The published checksum is named after the .osm.pbf download, so derive
    # it from the base name rather than from whatever suffix was passed.
    md5_base="$(basename "$PLANET")"
    md5_base="${md5_base%.crdownload}"
    md5_base="${md5_base%.osh.pbf}"
    md5_base="${md5_base%.osm.pbf}"
    md5_file="$PLANET_DIR/${md5_base}.osm.pbf.md5"
    if [ -f "$md5_file" ]; then
        say "   verifying md5 (a few minutes on 150 GB)..."
        run "(cd '$PLANET_DIR' && md5sum -c '$(basename "$md5_file")')" || die "md5 mismatch"
        ok "md5 verified"
    else
        warn "no $md5_file; skipping checksum"
    fi
fi

if [ "$PLANET" != "$OSH_PLANET" ]; then
    if [ -e "$OSH_PLANET" ]; then
        ok "already named $(basename "$OSH_PLANET")"
    else
        # Hardlink when possible (same filesystem, costs nothing) else copy.
        say "   naming as $(basename "$OSH_PLANET") (history flag detection)"
        run "ln '$PLANET' '$OSH_PLANET' 2>/dev/null || cp -a '$PLANET' '$OSH_PLANET'"
    fi
fi

# ── 2. tree ──────────────────────────────────────────────────────────────
say "2. tree"
run "mkdir -p '$TREE/OSM-PBF-FILES/planet' '$TREE/OSM-PBF-FILES/osm_polygon_files' '$TREE/OSM-PBF-FILES/world_kg_ontology' '$TREE/mapqa_parser'"

continents="$TREE/OSM-PBF-FILES/osm_wikidata_extractions/continents"
if [ -d "$continents" ] && [ -n "$(ls -A "$continents" 2>/dev/null || true)" ] && [ "$FORCE" = 0 ]; then
    die "tree already has continents ($continents); pass --force to reuse"
fi

run "ln '$OSH_PLANET' '$TREE/OSM-PBF-FILES/planet/$(basename "$OSH_PLANET")' 2>/dev/null || cp -a '$OSH_PLANET' '$TREE/OSM-PBF-FILES/planet/'"
ok "planet linked into the tree"

if [ -d "$POLY_SOURCE" ] && [ -n "$(ls -A "$POLY_SOURCE" 2>/dev/null || true)" ]; then
    run "cp -a '$POLY_SOURCE/.' '$TREE/OSM-PBF-FILES/osm_polygon_files/'"
    ok "polygons copied (precondition for extract_continents)"
else
    die "no polygons at $POLY_SOURCE - extract_continents cannot self-heal without them"
fi

if [ -d "$ONTOLOGY_SOURCE" ] && [ -n "$(ls -A "$ONTOLOGY_SOURCE" 2>/dev/null || true)" ]; then
    run "cp -a '$ONTOLOGY_SOURCE/.' '$TREE/OSM-PBF-FILES/world_kg_ontology/'"
    ok "ontology copied (enrich_worldkg_classes needs it)"
else
    warn "no ontology at $ONTOLOGY_SOURCE - enrich_worldkg_classes will fail"
fi

# ── 3. databases ─────────────────────────────────────────────────────────
if [ "$SKIP_DB" = 1 ]; then
    warn "3. databases skipped (--skip-db)"
else
    say "3. databases"
    create_db() {
        local db="$1" host="$2" port="$3" user="$4" password="$5"
        ${COMPOSE[@]} \
            -e TARGET_DB="$db" -e TARGET_HOST="$host" -e TARGET_PORT="$port" \
            -e TARGET_USER="$user" -e TARGET_PASSWORD="$password" \
            -e RUN_MIGRATIONS=false -e RUN_INIT_PLANET=false \
            "$SERVICE" python - <<'PY'
import os, psycopg2
from psycopg2 import sql
target = os.environ["TARGET_DB"]
_pw = os.environ["TARGET_PASSWORD"]
conn = psycopg2.connect(
    host=os.environ["TARGET_HOST"], port=os.environ["TARGET_PORT"],
    user=os.environ["TARGET_USER"], password=_pw,
    dbname="postgres",
)
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", [target])
    if cur.fetchone():
        print(f"  = {target} already exists")
    else:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
        print(f"  + created {target}")
PY
    }

    if [ "$DRY_RUN" = 1 ]; then
        echo "  [dry-run] create $DEFAULT_DB and $VECTORS_DB"
    else
        create_db "$DEFAULT_DB" "$POSTGRES_HOST" "$POSTGRES_PORT" \
                  "$POSTGRES_USER" "$POSTGRES_PASSWORD"
        create_db "$VECTORS_DB" "$PGVECTOR_HOST" "$PGVECTOR_PORT" \
                  "$PGVECTOR_USER" "$PGVECTOR_PASSWORD"
    fi

    say "   migrating both databases (includes core.0005 step_states)"
    run "${COMPOSE[*]} \
        -e POSTGRES_DB='$DEFAULT_DB' -e PGVECTOR_DB='$VECTORS_DB' \
        -e POSTGRES_HOST='$POSTGRES_HOST' -e PGVECTOR_HOST='$PGVECTOR_HOST' \
        -e POSTGRES_PORT='$POSTGRES_PORT' -e PGVECTOR_PORT='$PGVECTOR_PORT' \
        -e RUN_INIT_PLANET=false $SERVICE true"
    ok "migrated"
fi

# ── 4. build ─────────────────────────────────────────────────────────────
BUILD_NAME="planet-build-${DB_SUFFIX:-default}"
BUILD_ARGS=(
    -d --name "$BUILD_NAME"
    -v "$TREE:$CONTAINER_DATA_DIR"
    -v "$TREE/OSM-PBF-FILES:$CONTAINER_DATA_DIR/OSM-PBF-FILES"
    -e "BASE_DATA_DIR=$CONTAINER_DATA_DIR"
    -e "MAPQA_PARSER_DATA_DIR=$CONTAINER_DATA_DIR/mapqa_parser"
    -e "PLANET_OSM_FILE_PATH=$CONTAINER_DATA_DIR/OSM-PBF-FILES/planet/$(basename "$OSH_PLANET")"
    -e "SINGLE_SNAPSHOT_DATE=$SNAPSHOT_DATE"
    -e "POSTGRES_HOST=$POSTGRES_HOST" -e "PGVECTOR_HOST=$PGVECTOR_HOST"
    -e "REDIS_HOST=$REDIS_HOST"
    -e "POSTGRES_PORT=$POSTGRES_PORT" -e "PGVECTOR_PORT=$PGVECTOR_PORT"
    -e "POSTGRES_DB=$DEFAULT_DB" -e "PGVECTOR_DB=$VECTORS_DB"
)

if [ "$SKIP_BUILD" = 1 ]; then
    warn "4. build skipped (--skip-build)"
else
    say "4. build (detached; the entrypoint runs db_preflight -> migrate -> init_planet)"
    if [ "$DRY_RUN" = 1 ]; then
        echo "  [dry-run] $COMPOSE_CMD run ${BUILD_ARGS[*]} $SERVICE true"
    else
        "${COMPOSE[@]}" "${BUILD_ARGS[@]}" "$SERVICE" true
        ok "build started"
        echo
        echo "  watch:  docker logs -f $BUILD_NAME"
        echo "  resume: re-run the same docker compose command (skips completed steps)"
    fi
fi

cat <<EOF

Next:
  verify   $COMPOSE_CMD run --rm --no-deps \\
             -v "$TREE:$CONTAINER_DATA_DIR" \\
             -v "$TREE/OSM-PBF-FILES:$CONTAINER_DATA_DIR/OSM-PBF-FILES" \\
             -e BASE_DATA_DIR=$CONTAINER_DATA_DIR -e MAPQA_PARSER_DATA_DIR=$CONTAINER_DATA_DIR/mapqa_parser \\
             -e PLANET_OSM_FILE_PATH=$CONTAINER_DATA_DIR/OSM-PBF-FILES/planet/$(basename "$OSH_PLANET") \\
             -e POSTGRES_HOST=$POSTGRES_HOST -e PGVECTOR_HOST=$PGVECTOR_HOST \\
             -e REDIS_HOST=$REDIS_HOST -e POSTGRES_PORT=$POSTGRES_PORT -e PGVECTOR_PORT=$PGVECTOR_PORT \\
             -e POSTGRES_DB=$DEFAULT_DB -e PGVECTOR_DB=$VECTORS_DB \\
             -e RUN_MIGRATIONS=false -e RUN_INIT_PLANET=false \\
             $SERVICE python manage.py verify_planet_init

  expect   continent_profiles = 9, all continents .osh.pbf with with_history=true
EOF
