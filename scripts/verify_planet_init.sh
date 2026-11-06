#!/usr/bin/env bash
# Verify a planet-init build. Read-only; exits 1 on any failed check.
#
#   ./scripts/verify_planet_init.sh
#   ./scripts/verify_planet_init.sh --json
#   ./scripts/verify_planet_init.sh --skip-filesystem   # DB-only
#
# Filesystem checks need the data tree mounted, which compose.override.yml
# does by default. Pass --skip-filesystem when running somewhere the tree is
# absent (e.g. the web tier).
set -euo pipefail

exec docker compose run --rm --no-deps \
  -e RUN_MIGRATIONS=false \
  -e RUN_INIT_PLANET=false \
  backend python manage.py verify_planet_init "$@"
