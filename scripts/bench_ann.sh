#!/bin/bash
# bench_ann.sh — exact-vs-ANN latency + recall@20 benchmark for a leaf's
# gv_tags_embedding HNSW index (ISSUE-ann-gv-tags-flip, test plan §1).
#
# Promotes the ad-hoc 2026-10-23 probes into a repeatable check. Run
# before/after the `+ 0` flip and after each index build/REINDEX.
#
#   scripts/bench_ann.sh embeddings_2025_12_31_mx [--anchors N] [--runs K] [--ef-search N]
#
# What it reports:
#   1. leaf row count + HNSW indexes present
#   2. recall@20 per anchor: identity overlap (exact top-20 ∩ ANN top-20)
#      AND top-1 distance parity (ann_d1 - exact_d1). Identity overlap
#      understates recall on duplicate-heavy columns: thousands of rows
#      share one gv_tags embedding, so exact and ANN return arbitrary
#      20-twin subsets of the same cluster. The distance columns show
#      whether ANN reached an equally-good cluster — the search-relevant
#      bar — and the gap flags orphaned clusters.
#   3. latency: exact vs ANN ORDER BY ... LIMIT 20, K timed runs each
#
# Exact passes are full seq scans (~18 s each on an MX-class leaf); pick
# --anchors accordingly.

set -e

LEAF="${1:-}"
if [ -z "$LEAF" ]; then
    echo "usage: $0 <leaf_table> [--anchors N] [--runs K] [--ef-search N]" >&2
    exit 2
fi
shift

ANCHORS=10
RUNS=3
EF=40
while [ $# -gt 0 ]; do
    case "$1" in
        --anchors)   ANCHORS="$2"; shift 2 ;;
        --runs)      RUNS="$2";    shift 2 ;;
        --ef-search) EF="$2";      shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Load .env for PGVECTOR_USER / PGVECTOR_DB (same convention as
# scripts/drop_databases_v2.sh).
if [ -f .env.local ]; then
    set -a; source .env.local; set +a
elif [ -f .env ]; then
    set -a; source .env; set +a
fi

DOCKER_COMPOSE="docker compose -f docker-compose.yml -f compose.override.yml"
PSQL="$DOCKER_COMPOSE exec -T postgres-vectors psql -U ${PGVECTOR_USER} -d ${PGVECTOR_DB} -v ON_ERROR_STOP=1 -P pager=off"

echo "== leaf: $LEAF =="
$PSQL -c "SELECT count(*) AS rows FROM $LEAF;" \
      -c "SELECT indexname FROM pg_indexes WHERE tablename = '$LEAF' AND indexdef LIKE '%hnsw%';"

echo "== recall@20: $ANCHORS anchors, ef_search=$EF (exact top-20 per anchor — slow) =="
$PSQL <<SQL
SET jit = off;
SET hnsw.ef_search = $EF;
WITH anchors AS (
  SELECT osm_type AS a_t, osm_id AS a_i, gv_tags_embedding AS q
  FROM $LEAF TABLESAMPLE SYSTEM (0.01)
  WHERE gv_tags_embedding IS NOT NULL
  LIMIT $ANCHORS
),
exact_top AS (
  SELECT a.a_t, a.a_i, e.osm_type, e.osm_id, e.d
  FROM anchors a
  CROSS JOIN LATERAL (
    SELECT x.osm_type, x.osm_id, (x.gv_tags_embedding <=> a.q) + 0 AS d
    FROM $LEAF x
    WHERE x.gv_tags_embedding IS NOT NULL
    ORDER BY d
    LIMIT 20
  ) e
),
ann_top AS (
  SELECT a.a_t, a.a_i, e.osm_type, e.osm_id, e.d
  FROM anchors a
  CROSS JOIN LATERAL (
    SELECT x.osm_type, x.osm_id, x.gv_tags_embedding <=> a.q AS d
    FROM $LEAF x
    WHERE x.gv_tags_embedding IS NOT NULL
    ORDER BY d
    LIMIT 20
  ) e
)
SELECT a.a_t AS anchor_type, a.a_i AS anchor_id,
       (SELECT count(*) FROM exact_top e JOIN ann_top n
          ON (n.osm_type, n.osm_id) = (e.osm_type, e.osm_id)
        WHERE (e.a_t, e.a_i) = (a.a_t, a.a_i)
          AND (n.a_t, n.a_i) = (a.a_t, a.a_i)) AS overlap_20,
       (SELECT min(d) FROM exact_top e
         WHERE (e.a_t, e.a_i) = (a.a_t, a.a_i)) AS exact_d1,
       (SELECT min(d) FROM ann_top n
         WHERE (n.a_t, n.a_i) = (a.a_t, a.a_i)) AS ann_d1,
       (SELECT max(d) FROM exact_top e
         WHERE (e.a_t, e.a_i) = (a.a_t, a.a_i)) AS exact_d20,
       (SELECT max(d) FROM ann_top n
         WHERE (n.a_t, n.a_i) = (a.a_t, a.a_i)) AS ann_d20
FROM anchors a
ORDER BY overlap_20, a.a_t, a.a_i;
SQL

echo "== latency: $RUNS run(s) each, exact (+0) then ANN =="
for i in $(seq "$RUNS"); do
    echo "-- run $i"
    $PSQL <<SQL
SET jit = off;
SET hnsw.ef_search = $EF;
SELECT gv_tags_embedding::text AS qvec FROM $LEAF
 WHERE gv_tags_embedding IS NOT NULL ORDER BY osm_id LIMIT 1 \gset
\timing on
SELECT count(*) AS exact_top20 FROM (
  SELECT osm_id FROM $LEAF
  ORDER BY (gv_tags_embedding <=> :'qvec'::vector) + 0 LIMIT 20
) s;
SELECT count(*) AS ann_top20 FROM (
  SELECT osm_id FROM $LEAF
  ORDER BY gv_tags_embedding <=> :'qvec'::vector LIMIT 20
) s;
\timing off
SQL
done

echo "== done =="
