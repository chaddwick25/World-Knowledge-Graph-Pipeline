#!/bin/bash
# reset_worldkg_db.sh — drop and recreate ONE WorldKG database generation on the
# k80 data tier, then exercise the real container entrypoint
# (db_preflight -> migrate -> init_planet) so the init chain runs from scratch.
#
# WHY THIS EXISTS
#   scripts/drop_databases_v2.sh targets the pre-split single-host layout
#   (local `postgres-default`/`postgres-vectors` containers, `sudo rm -rf` of
#   the whole cluster data dir). The live data tier is on the k80 as
#   `worldkg-pg-*`, and its clusters hold several generations side by side, so
#   that script is both wrong-target and far too broad.
#
# WHAT IT DOES NOT TOUCH
#   The on-disk OSM tree. The planet PBF, the nine continent extracts, the poly
#   files and the GeoVectors TSVs are already preprocessed and are deliberately
#   left alone — `extract_continents` therefore short-circuits in seconds and
#   nothing is re-extracted.
#
# USAGE
#   ./scripts/reset_worldkg_db.sh --dry-run          # print the plan, change nothing
#   ./scripts/reset_worldkg_db.sh                    # interactive confirm, then run
#   ./scripts/reset_worldkg_db.sh --yes              # non-interactive
#   ./scripts/reset_worldkg_db.sh --yes --skip-init  # drop + recreate only
#   ./scripts/reset_worldkg_db.sh --generation v2 --yes --i-know-about-belize
#
# ENV OVERRIDES
#   WORLDKG_PG_HOST        REQUIRED - tailnet address of the data tier. Not
#                          defaulted: host identity must not live in a tracked
#                          file. Export it in your shell or a local env file.
#   WORLDKG_PG_CONTAINER   default worldkg-pg-default
#   WORLDKG_VEC_CONTAINER  default worldkg-pg-vectors
#   WORLDKG_DJANGO_USER    default django_user
#   WORLDKG_VECTOR_USER    default vector_user
#   WORLDKG_DATA_DIR       default /media/thanos/2TB_NVME/worldkg_v3

set -euo pipefail

PG_HOST="${WORLDKG_PG_HOST:?set WORLDKG_PG_HOST to the tailnet address of the data tier}"
PG_CONTAINER="${WORLDKG_PG_CONTAINER:-worldkg-pg-default}"
VEC_CONTAINER="${WORLDKG_VEC_CONTAINER:-worldkg-pg-vectors}"
REDIS_CONTAINER="${WORLDKG_REDIS_CONTAINER:-worldkg-redis}"
DJANGO_USER="${WORLDKG_DJANGO_USER:-django_user}"
VECTOR_USER="${WORLDKG_VECTOR_USER:-vector_user}"
DATA_DIR="${WORLDKG_DATA_DIR:-/media/thanos/2TB_NVME/worldkg_v3}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

GEN="v3"
ASSUME_YES=0
DRY_RUN=0
DO_BACKUP=1
SKIP_INIT=0
FLUSH_REDIS=0
BELIZE_ACK=0

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YEL=$'\033[1;33m'; BLU=$'\033[0;34m'; NC=$'\033[0m'
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==> %s%s\n' "$BLU" "$*" "$NC"; }
warn() { printf '%sWARN: %s%s\n' "$YEL" "$*" "$NC"; }
die()  { printf '%sERROR: %s%s\n' "$RED" "$*" "$NC" >&2; exit 1; }

usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --generation)     GEN="${2:?--generation needs a value}"; shift 2 ;;
        --yes|-y)         ASSUME_YES=1; shift ;;
        --dry-run)        DRY_RUN=1; shift ;;
        --no-backup)      DO_BACKUP=0; shift ;;
        --skip-init)      SKIP_INIT=1; shift ;;
        --flush-redis)    FLUSH_REDIS=1; shift ;;
        --i-know-about-belize) BELIZE_ACK=1; shift ;;
        --help|-h)        usage ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

case "$GEN" in
    v3) DJANGO_DB="django_db_v3"; VECTOR_DB="vector_db_v3"
        DEFAULT_TREE="/media/thanos/2TB_NVME/worldkg_v3"
        DEFAULT_PLANET="history-261102.osh.pbf" ;;
    v2) DJANGO_DB="django_db_v2"; VECTOR_DB="vector_db_v2"
        DEFAULT_TREE="/media/thanos/2TB_NVME/worldkg_v2"
        DEFAULT_PLANET="history-261026.osh.pbf" ;;
    v1) DJANGO_DB="django_db";    VECTOR_DB="vector_db"
        DEFAULT_TREE="/media/thanos/2TB_NVME"
        DEFAULT_PLANET="history-261102.osh.pbf" ;;
    *)  die "--generation must be one of: v1 v2 v3" ;;
esac

# The data tree must match the generation: each one holds a different planet
# file and its own continent extracts. Pointing a v2 DB at the v3 tree would
# silently register the wrong planet.
DATA_DIR="${WORLDKG_DATA_DIR:-$DEFAULT_TREE}"
PLANET_PBF="${WORLDKG_PLANET_PBF:-$DEFAULT_PLANET}"
PLANET_PATH="/app/data/OSM-PBF-FILES/planet/$PLANET_PBF"

[ -d "$DATA_DIR" ] || die "data tree not found: $DATA_DIR"
[ -f "$DATA_DIR/OSM-PBF-FILES/planet/$PLANET_PBF" ] \
    || die "planet file not found: $DATA_DIR/OSM-PBF-FILES/planet/$PLANET_PBF"

# --- guard: v2 holds the only real data in the system -----------------------
if [ "$GEN" = "v2" ] && [ "$BELIZE_ACK" -ne 1 ]; then
    die "vector_db_v2 holds 307,938 Belize entities — the only populated data.
     Re-run with --generation v2 --i-know-about-belize if you really mean it."
fi
if [ "$DO_BACKUP" -eq 0 ] && [ "$ASSUME_YES" -ne 1 ]; then
    die "--no-backup requires --yes"
fi

# --- remote psql helper ------------------------------------------------------
# q <container> <user> <database> <sql>
q() {
    local container="$1" user="$2" db="$3" sql="$4"
    ssh -o BatchMode=yes -o ConnectTimeout=8 "$PG_HOST" \
        "docker exec -i $container psql -U $user -d $db -v ON_ERROR_STOP=1 -tAc $(printf '%q' "$sql")"
}

# --- preflight ---------------------------------------------------------------
step "Preflight"
ssh -o BatchMode=yes -o ConnectTimeout=8 "$PG_HOST" 'true' \
    || die "cannot ssh to $PG_HOST"

for c in "$PG_CONTAINER" "$VEC_CONTAINER"; do
    state=$(ssh -o BatchMode=yes "$PG_HOST" "docker inspect -f '{{.State.Status}}' $c 2>/dev/null" || echo missing)
    [ "$state" = "running" ] || die "container $c is '$state' on $PG_HOST — start the data tier first"
done
say "  data tier reachable: $PG_HOST ($PG_CONTAINER, $VEC_CONTAINER)"

# --- show current state ------------------------------------------------------
step "Current state"
d_size=$(q "$PG_CONTAINER" "$DJANGO_USER" postgres \
    "SELECT pg_size_pretty(pg_database_size('$DJANGO_DB'))" 2>/dev/null || echo "absent")
v_size=$(q "$VEC_CONTAINER" "$VECTOR_USER" postgres \
    "SELECT pg_size_pretty(pg_database_size('$VECTOR_DB'))" 2>/dev/null || echo "absent")
v_entities=$(q "$VEC_CONTAINER" "$VECTOR_USER" "$VECTOR_DB" \
    "SELECT count(*) FROM semantic_search_osmentity" 2>/dev/null || echo "n/a")
d_snaps=$(q "$PG_CONTAINER" "$DJANGO_USER" "$DJANGO_DB" \
    "SELECT count(*) FROM planet_snapshots" 2>/dev/null || echo "n/a")

printf '  %-16s %-9s planet_snapshots=%s\n' "$DJANGO_DB:" "$d_size" "$d_snaps"
printf '  %-16s %-9s semantic_search_osmentity=%s\n' "$VECTOR_DB:" "$v_size" "$v_entities"

say ""
say "  Generation:   $GEN"
say "  Data tree:    $DATA_DIR"
say "  Planet PBF:   $PLANET_PBF"
say ""
say "  Will DROP and recreate:"
say "    $DJANGO_DB   (owner $DJANGO_USER)"
say "    $VECTOR_DB   (owner $VECTOR_USER)"
say "  On-disk OSM data is NOT touched."

if [ "$DRY_RUN" -eq 1 ]; then
    say ""
    say "${GRN}--dry-run: nothing changed.${NC}"
    exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
    printf '\n%sType "yes" to proceed: %s' "$YEL" "$NC"
    read -r reply
    [ "$reply" = "yes" ] || { say "${GRN}Aborted.${NC}"; exit 0; }
fi

# --- backup ------------------------------------------------------------------
if [ "$DO_BACKUP" -eq 1 ]; then
    step "Backup"
    TS=$(date -u +%Y%m%dT%H%M%SZ)
    BDIR="\$HOME/backup/reset-${TS}"
    ssh -o BatchMode=yes "$PG_HOST" "mkdir -p $BDIR"
    say "  -> $PG_HOST:~/backup/reset-${TS}/"

    # No -t on docker exec: a TTY corrupts the custom-format archive.
    ssh -o BatchMode=yes "$PG_HOST" \
        "docker exec $PG_CONTAINER pg_dump -U $DJANGO_USER -Fc $DJANGO_DB > $BDIR/${DJANGO_DB}.dump"
    ssh -o BatchMode=yes "$PG_HOST" \
        "docker exec $VEC_CONTAINER pg_dump -U $VECTOR_USER -Fc $VECTOR_DB > $BDIR/${VECTOR_DB}.dump"
    ssh -o BatchMode=yes "$PG_HOST" \
        "docker exec $PG_CONTAINER pg_dumpall -U $DJANGO_USER --globals-only > $BDIR/globals.sql"

    ssh -o BatchMode=yes "$PG_HOST" "ls -lh $BDIR"
    # Fail the run if an archive header is unreadable.
    ssh -o BatchMode=yes "$PG_HOST" \
        "docker exec -i $PG_CONTAINER pg_restore -l < $BDIR/${DJANGO_DB}.dump | head -1" >/dev/null \
        || die "backup of $DJANGO_DB did not produce a readable archive — aborting before the drop"
    ssh -o BatchMode=yes "$PG_HOST" \
        "docker exec -i $VEC_CONTAINER pg_restore -l < $BDIR/${VECTOR_DB}.dump | head -1" >/dev/null \
        || die "backup of $VECTOR_DB did not produce a readable archive — aborting before the drop"
    say "  ${GRN}backups verified${NC}"
else
    warn "skipping backup (--no-backup)"
fi

# --- drop + recreate ---------------------------------------------------------
step "Drop and recreate"
for spec in "$PG_CONTAINER:$DJANGO_USER:$DJANGO_DB" "$VEC_CONTAINER:$VECTOR_USER:$VECTOR_DB"; do
    IFS=: read -r cont user db <<< "$spec"
    say "  terminating connections to $db"
    q "$cont" "$user" postgres \
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$db' AND pid <> pg_backend_pid()" >/dev/null
    say "  DROP DATABASE $db"
    q "$cont" "$user" postgres "DROP DATABASE IF EXISTS $db" >/dev/null
    say "  CREATE DATABASE $db OWNER $user"
    q "$cont" "$user" postgres "CREATE DATABASE $db OWNER $user" >/dev/null
done

if [ "$FLUSH_REDIS" -eq 1 ]; then
    say "  FLUSHALL on $REDIS_CONTAINER"
    ssh -o BatchMode=yes "$PG_HOST" "docker exec $REDIS_CONTAINER redis-cli FLUSHALL" >/dev/null
fi

say "  ${GRN}databases recreated empty${NC}"

if [ "$SKIP_INIT" -eq 1 ]; then
    say ""
    say "${GRN}--skip-init: databases are empty. Run the entrypoint yourself:${NC}"
    say "  docker compose run --rm --no-deps \\"
    say "    -e POSTGRES_DB=$DJANGO_DB -e PGVECTOR_DB=$VECTOR_DB ... backend python manage.py check"
    exit 0
fi

# --- run the real entrypoint path -------------------------------------------
# RUN_MIGRATIONS / RUN_INIT_PLANET are deliberately NOT set: the entrypoint then
# takes the backend branch and does db_preflight -> migrate -> init_planet,
# which is the exact sequence we want to prove.
step "Entrypoint: db_preflight -> migrate -> init_planet"
say "  this is the long part (expect ~10-20 min; continents short-circuit)"
cd "$REPO_ROOT"
docker compose run --rm --no-deps \
    -v "${DATA_DIR}:/app/data" \
    -v "${DATA_DIR}/OSM-PBF-FILES:/app/data/OSM-PBF-FILES" \
    -e BASE_DATA_DIR=/app/data \
    -e MAPQA_PARSER_DATA_DIR=/app/data/mapqa_parser \
    -e PLANET_OSM_FILE_PATH="$PLANET_PATH" \
    -e POSTGRES_HOST="$PG_HOST" -e PGVECTOR_HOST="$PG_HOST" -e REDIS_HOST="$PG_HOST" \
    -e POSTGRES_PORT=5432 -e PGVECTOR_PORT=5433 \
    -e POSTGRES_DB="$DJANGO_DB" -e PGVECTOR_DB="$VECTOR_DB" \
    -e POSTGRES_USER="$DJANGO_USER" -e PGVECTOR_USER="$VECTOR_USER" \
    backend python manage.py check

# --- verify ------------------------------------------------------------------
step "Verify"
docker compose run --rm --no-deps \
    -v "${DATA_DIR}:/app/data" \
    -v "${DATA_DIR}/OSM-PBF-FILES:/app/data/OSM-PBF-FILES" \
    -e BASE_DATA_DIR=/app/data \
    -e MAPQA_PARSER_DATA_DIR=/app/data/mapqa_parser \
    -e PLANET_OSM_FILE_PATH="$PLANET_PATH" \
    -e POSTGRES_HOST="$PG_HOST" -e PGVECTOR_HOST="$PG_HOST" -e REDIS_HOST="$PG_HOST" \
    -e POSTGRES_PORT=5432 -e PGVECTOR_PORT=5433 \
    -e POSTGRES_DB="$DJANGO_DB" -e PGVECTOR_DB="$VECTOR_DB" \
    -e POSTGRES_USER="$DJANGO_USER" -e PGVECTOR_USER="$VECTOR_USER" \
    -e RUN_MIGRATIONS=false -e RUN_INIT_PLANET=false \
    backend python manage.py verify_planet_init || true

step "Run state (the thing --step runs never recorded)"
q "$PG_CONTAINER" "$DJANGO_USER" "$DJANGO_DB" \
    "SELECT snapshot_date_str, status, step_states FROM planet_snapshots ORDER BY created_at DESC LIMIT 2" \
    || warn "could not read planet_snapshots"

say ""
say "${GRN}done.${NC} Backups: $PG_HOST:~/backup/reset-${TS:-<ts>}/"
