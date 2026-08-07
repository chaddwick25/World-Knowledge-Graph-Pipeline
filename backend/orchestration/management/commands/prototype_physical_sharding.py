"""Physical temporal sharding prototype — prove the partition mechanism end-to-end.

Creates a REAL partitioned ``embeddings_partitioned`` table in the vectors DB,
migrates one country's data (CU — Cuba, 190K entities) from the monolith into
a leaf partition, builds an HNSW index on the leaf, creates a materialized
merge view, and verifies:

  1. Partition pruning (querying by snapshot_id prunes to one partition)
  2. ANN search via the leaf's HNSW index
  3. Materialized view merge (country-level query via the view)
  4. Parallel-safe upsert via ON CONFLICT into the leaf
  5. PartitionRegistry tracking

This is NON-DESTRUCTIVE — it creates new tables, does not touch the existing
``semantic_search_osmentity`` monolith. Use ``--cleanup`` to drop everything.

Usage::

    python manage.py prototype_physical_sharding
    python manage.py prototype_physical_sharding --country CU --snapshot 2025_12_31
    python manage.py prototype_physical_sharding --cleanup
    python manage.py prototype_physical_sharding --skip-data   # structure only

Implements the recommended pre-Phase-6 prototype from
``docs/plans/TEMPORAL_SHARDING_IMPLEMENTATION.md``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from django.core.management.base import BaseCommand
from django.db import connections

from orchestration.models import PartitionRegistry

logger = logging.getLogger(__name__)

# Bounding boxes for countries with COMPLETED pipeline runs.
# Used to spatially filter entities from the monolith (which has no
# country_code column) into the correct leaf partition.
COUNTRY_BBOXES = {
    "CU": (-85.0, 19.6, -74.0, 23.6),   # Cuba — 190K entities
    "CV": (-25.4, 14.5, -22.4, 17.4),   # Cape Verde — 38K
    "IS": (-25.0, 63.0, -13.0, 67.0),   # Iceland — 184K
    "JM": (-79.0, 17.5, -76.0, 18.7),   # Jamaica — 30K
}

ROOT_TABLE = "embeddings_partitioned"
SNAPSHOT_TABLE = "embeddings_{snapshot}"          # e.g. embeddings_2025_12_31
LEAF_TABLE = "embeddings_{snapshot}_{cc_lower}"   # e.g. embeddings_2025_12_31_cu
MV_TABLE = "mv_embeddings_{snapshot}_{cc_lower}"  # e.g. mv_embeddings_2025_12_31_cu


class Command(BaseCommand):
    help = (
        "Physical temporal sharding prototype: create partitioned table, "
        "migrate one country's data, build HNSW + materialized view, verify."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--country", default="CU",
            help="ISO 3166-1 alpha-2 country code to migrate (default: CU). "
                 "Must be in COUNTRY_BBOXES.",
        )
        parser.add_argument(
            "--snapshot", default="2025_12_31",
            help="Snapshot id, YYYY_MM_DD format (default: 2025_12_31).",
        )
        parser.add_argument(
            "--cleanup", action="store_true",
            help="Drop all prototype tables and the materialized view, then exit.",
        )
        parser.add_argument(
            "--skip-data", action="store_true",
            help="Create the partition structure but skip data migration.",
        )
        parser.add_argument(
            "--hnsw-m", type=int, default=16,
            help="HNSW M parameter for the leaf index (default: 16).",
        )
        parser.add_argument(
            "--hnsw-ef-construction", type=int, default=128,
            help="HNSW ef_construction (default: 128).",
        )

    def handle(self, *args, **options):
        country = options["country"].upper()
        snapshot = options["snapshot"]
        cleanup = options["cleanup"]
        skip_data = options["skip_data"]
        m = options["hnsw_m"]
        ef_construction = options["hnsw_ef_construction"]

        if cleanup:
            self._cleanup(snapshot, country)
            return

        if country not in COUNTRY_BBOXES:
            self.stdout.write(self.style.ERROR(
                f"Country {country} not in COUNTRY_BBOXES. "
                f"Available: {list(COUNTRY_BBOXES.keys())}"
            ))
            return

        bbox = COUNTRY_BBOXES[country]
        cc_lower = country.lower()
        snap_table = SNAPSHOT_TABLE.format(snapshot=snapshot)
        leaf_table = LEAF_TABLE.format(snapshot=snapshot, cc_lower=cc_lower)
        mv_table = MV_TABLE.format(snapshot=snapshot, cc_lower=cc_lower)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Physical sharding prototype: {country} / {snapshot}\n"
            f"  root={ROOT_TABLE} snapshot={snap_table} leaf={leaf_table} mv={mv_table}"
        ))
        self.stdout.write("-" * 72)

        with connections["vectors"].cursor() as cursor:
            # ── Phase 1: Create the partitioned table hierarchy ────────────
            self._phase1_create_partitions(cursor, snapshot, country, snap_table, leaf_table)

            # ── Phase 2: Migrate data from monolith → leaf ─────────────────
            if not skip_data:
                self._phase2_migrate_data(cursor, country, bbox, snapshot, leaf_table)
            else:
                self.stdout.write(self.style.WARNING("[2] Skipping data migration (--skip-data)"))

            # ── Phase 3: Build HNSW index on the leaf ──────────────────────
            if not skip_data:
                self._phase3_build_hnsw(cursor, leaf_table, m, ef_construction)
            else:
                self.stdout.write(self.style.WARNING("[3] Skipping HNSW build (--skip-data)"))

            # ── Phase 4: Create materialized merge view ────────────────────
            self._phase4_create_mview(cursor, leaf_table, mv_table, country, snapshot)

        # ── Phase 5: Verify — partition pruning, ANN, upsert ──────────────
        if not skip_data:
            self._phase5_verify(cursor_fn=lambda: connections["vectors"].cursor(),
                                snap_table=snap_table, leaf_table=leaf_table,
                                mv_table=mv_table, country=country, snapshot=snapshot)

        # ── Phase 6: PartitionRegistry tracking ───────────────────────────
        if not skip_data:
            self._phase6_partition_registry(snapshot, country, leaf_table)

        self.stdout.write("-" * 72)
        self.stdout.write(self.style.SUCCESS(
            f"Prototype complete. Leaf: {leaf_table} | MV: {mv_table}\n"
            f"  Run with --cleanup to drop all prototype tables."
        ))

    # ── Phase 1: Create partitioned table + partitions ────────────────────

    def _phase1_create_partitions(self, cursor, snapshot, country, snap_table, leaf_table):
        self.stdout.write(self.style.MIGRATE_HEADING(
            "[1] Creating partitioned table hierarchy"
        ))

        # Root: partitioned by LIST(snapshot_id)
        # PK must include partition key (Postgres requirement).
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {ROOT_TABLE} (
                id BIGINT NOT NULL,
                snapshot_id VARCHAR(20) NOT NULL,
                country_code VARCHAR(3) NOT NULL,
                subdivision VARCHAR(255),
                osm_type VARCHAR(10) NOT NULL,
                osm_id BIGINT NOT NULL,
                tags JSONB NOT NULL,
                gv_tags_embedding VECTOR(300),
                gv_nle_embedding VECTOR(100),
                static_embedding VECTOR(400),
                wkg_class VARCHAR(200),
                wkg_superclasses VARCHAR(200)[],
                wikidata_uri VARCHAR(200),
                wkg_depth INT,
                wkg_type_key VARCHAR(100),
                wkg_type_value VARCHAR(100),
                wkg_enriched_at TIMESTAMPTZ,
                geom GEOMETRY(Point, 4326),
                version INT,
                timestamp TIMESTAMPTZ,
                gv_tags_version VARCHAR(50) NOT NULL DEFAULT '1.0',
                gv_nle_version VARCHAR(50),
                gv_nle_trained BOOLEAN NOT NULL DEFAULT FALSE,
                source_snapshot_id UUID,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (id, snapshot_id, country_code)
            ) PARTITION BY LIST (snapshot_id);
        """)
        self.stdout.write(f"  root: {ROOT_TABLE} (PARTITION BY LIST(snapshot_id))")

        # Unique constraint must include ALL partition key columns.
        # (osm_type, osm_id, gv_tags_version) was the monolith's unique —
        # in the partitioned table we add snapshot_id + country_code.
        cursor.execute(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_{ROOT_TABLE}_entity_unique
            ON {ROOT_TABLE} (osm_type, osm_id, gv_tags_version, snapshot_id, country_code);
        """)
        self.stdout.write(f"  unique index: (osm_type, osm_id, gv_tags_version, snapshot_id, country_code)")

        # Snapshot partition: sub-partitioned by LIST(country_code)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {snap_table}
            PARTITION OF {ROOT_TABLE}
            FOR VALUES IN ('{snapshot}')
            PARTITION BY LIST (country_code);
        """)
        self.stdout.write(f"  snapshot partition: {snap_table} (PARTITION BY LIST(country_code))")

        # Country leaf: CU is a small country, no subdivision sub-partitioning.
        # This IS the leaf.
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {leaf_table}
            PARTITION OF {snap_table}
            FOR VALUES IN ('{country}');
        """)
        self.stdout.write(f"  leaf: {leaf_table} (FOR VALUES IN ('{country}')) — no sub-partitioning (single-leaf country)")

        # B-tree indexes on the leaf (match the monolith's query patterns)
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{leaf_table}_osm ON {leaf_table} (osm_type, osm_id);")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{leaf_table}_geom ON {leaf_table} USING gist (geom);")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{leaf_table}_wkg_class ON {leaf_table} (wkg_class);")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{leaf_table}_wikidata ON {leaf_table} (wikidata_uri);")
        self.stdout.write(f"  btree+gist indexes on leaf: OK")

    # ── Phase 2: Migrate data from monolith → leaf ────────────────────────

    def _phase2_migrate_data(self, cursor, country, bbox, snapshot, leaf_table):
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"[2] Migrating {country} entities from monolith → {leaf_table}"
        ))
        lon_min, lat_min, lon_max, lat_max = bbox

        # Count source entities
        cursor.execute(f"""
            SELECT count(*) FROM semantic_search_osmentity
            WHERE geom IS NOT NULL
              AND geom && ST_MakeEnvelope({lon_min}, {lat_min}, {lon_max}, {lat_max}, 4326);
        """)
        count = cursor.fetchone()[0]
        self.stdout.write(f"  source entities (spatial filter): {count}")
        if count == 0:
            self.stdout.write(self.style.ERROR("  No entities found — aborting migration."))
            return

        # Bulk INSERT ... SELECT from monolith into the leaf.
        # Adds snapshot_id, country_code, subdivision=NULL.
        # Uses ON CONFLICT to be idempotent (re-runnable).
        cursor.execute(f"""
            INSERT INTO {leaf_table} (
                id, snapshot_id, country_code, subdivision,
                osm_type, osm_id, tags,
                gv_tags_embedding, gv_nle_embedding, static_embedding,
                wkg_class, wkg_superclasses, wikidata_uri, wkg_depth,
                wkg_type_key, wkg_type_value, wkg_enriched_at,
                geom, version, timestamp,
                gv_tags_version, gv_nle_version, gv_nle_trained,
                source_snapshot_id, created_at, updated_at
            )
            SELECT
                e.id, '{snapshot}', '{country}', NULL,
                e.osm_type, e.osm_id, e.tags,
                e.gv_tags_embedding, e.gv_nle_embedding, e.static_embedding,
                e.wkg_class, e.wkg_superclasses, e.wikidata_uri, e.wkg_depth,
                e.wkg_type_key, e.wkg_type_value, e.wkg_enriched_at,
                e.geom, e.version, e.timestamp,
                e.gv_tags_version, e.gv_nle_version, e.gv_nle_trained,
                e.source_snapshot_id, e.created_at, e.updated_at
            FROM semantic_search_osmentity e
            WHERE e.geom IS NOT NULL
              AND e.geom && ST_MakeEnvelope({lon_min}, {lat_min}, {lon_max}, {lat_max}, 4326)
            ON CONFLICT (osm_type, osm_id, gv_tags_version, snapshot_id, country_code)
            DO UPDATE SET
                tags = EXCLUDED.tags,
                gv_tags_embedding = EXCLUDED.gv_tags_embedding,
                gv_nle_embedding = EXCLUDED.gv_nle_embedding,
                static_embedding = EXCLUDED.static_embedding,
                geom = EXCLUDED.geom,
                updated_at = NOW();
        """)

        # Verify row count
        cursor.execute(f"SELECT count(*) FROM {leaf_table};")
        migrated = cursor.fetchone()[0]
        self.stdout.write(self.style.SUCCESS(
            f"  migrated: {migrated} rows into {leaf_table}"
        ))

        # Count static_embedding coverage
        cursor.execute(f"""
            SELECT count(*) FROM {leaf_table} WHERE static_embedding IS NOT NULL;
        """)
        with_static = cursor.fetchone()[0]
        self.stdout.write(f"  with static_embedding: {with_static}/{migrated}")

    # ── Phase 3: Build HNSW index on the leaf ─────────────────────────────

    def _phase3_build_hnsw(self, cursor, leaf_table, m, ef_construction):
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"[3] Building HNSW index on {leaf_table}.static_embedding"
        ))
        idx_name = f"idx_{leaf_table}_hnsw"
        cursor.execute(f"SET max_parallel_maintenance_workers = 1;")
        cursor.execute(f"""
            CREATE INDEX IF NOT EXISTS {idx_name}
            ON {leaf_table}
            USING hnsw (static_embedding vector_cosine_ops)
            WITH (m = {m}, ef_construction = {ef_construction});
        """)
        cursor.execute(f"""
            SELECT pg_size_pretty(pg_relation_size('{idx_name}'));
        """)
        size = cursor.fetchone()[0]
        self.stdout.write(self.style.SUCCESS(
            f"  HNSW index: {idx_name} (m={m}, ef_construction={ef_construction}, size={size})"
        ))

    # ── Phase 4: Create materialized merge view ───────────────────────────

    def _phase4_create_mview(self, cursor, leaf_table, mv_table, country, snapshot):
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"[4] Creating materialized view {mv_table}"
        ))
        # For a single-leaf country, the MV is just a SELECT * from the leaf.
        # For multi-leaf countries (MZ with subdivisions), this would be
        # UNION ALL of all subdivision leaves — that's the merge mechanism.
        cursor.execute(f"DROP MATERIALIZED VIEW IF EXISTS {mv_table};")
        cursor.execute(f"""
            CREATE MATERIALIZED VIEW {mv_table} AS
            SELECT * FROM {leaf_table};
        """)
        # The MV gets its own HNSW index for country-level ANN queries.
        # MVs do NOT inherit indexes from their underlying tables — this
        # must be created explicitly. This is the cost of the merge-at-
        # query-time mechanism: each REFRESH MATERIALIZED VIEW requires
        # rebuilding this index (or use REFRESH ... CONCURRENTLY which
        # needs a unique index + doesn't rebuild if data unchanged).
        cursor.execute(f"""
            CREATE UNIQUE INDEX idx_{mv_table}_id
            ON {mv_table} (id);
        """)
        cursor.execute(f"SET max_parallel_maintenance_workers = 1;")
        cursor.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{mv_table}_hnsw
            ON {mv_table}
            USING hnsw (static_embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 128);
        """)
        cursor.execute(f"SELECT pg_size_pretty(pg_relation_size('idx_{mv_table}_hnsw'));")
        mv_hnsw_size = cursor.fetchone()[0]
        self.stdout.write(f"  MV HNSW index: idx_{mv_table}_hnsw (size={mv_hnsw_size})")
        cursor.execute(f"""
            SELECT count(*) FROM {mv_table};
        """)
        count = cursor.fetchone()[0]
        self.stdout.write(self.style.SUCCESS(
            f"  materialized view: {mv_table} ({count} rows)\n"
            f"  NOTE: For multi-leaf countries, this would be UNION ALL of\n"
            f"  all subdivision leaves — the merge-at-query-time mechanism."
        ))

    # ── Phase 5: Verify — partition pruning, ANN, upsert ──────────────────

    def _phase5_verify(self, cursor_fn, snap_table, leaf_table, mv_table, country, snapshot):
        self.stdout.write(self.style.MIGRATE_HEADING(
            "[5] Verification — partition pruning, ANN, upsert"
        ))
        with cursor_fn() as cursor:
            cursor.execute("SET jit = off;")

            # 5a. Partition pruning — query root with snapshot filter
            self.stdout.write("  [5a] Partition pruning (query root with snapshot filter):")
            cursor.execute(f"""
                EXPLAIN (FORMAT text)
                SELECT count(*) FROM {ROOT_TABLE}
                WHERE snapshot_id = '{snapshot}';
            """)
            plan = cursor.fetchall()
            for row in plan:
                if "Seq Scan" in row[0] or "Index" in row[0] or leaf_table in row[0] or snap_table in row[0]:
                    self.stdout.write(f"    {row[0].strip()}")

            # 5b. Partition pruning — query root with snapshot + country filter
            self.stdout.write(f"  [5b] Partition pruning (snapshot + country={country}):")
            cursor.execute(f"""
                EXPLAIN (FORMAT text)
                SELECT count(*) FROM {ROOT_TABLE}
                WHERE snapshot_id = '{snapshot}' AND country_code = '{country}';
            """)
            plan = cursor.fetchall()
            for row in plan:
                if leaf_table in row[0] or "Seq Scan" in row[0] or "Aggregate" in row[0]:
                    self.stdout.write(f"    {row[0].strip()}")

            # 5c. ANN query via leaf HNSW
            self.stdout.write("  [5c] ANN query via leaf HNSW (top-3, ef_search=200):")
            cursor.execute("SET hnsw.ef_search = 200;")
            cursor.execute(f"""
                EXPLAIN (ANALYZE, FORMAT text)
                SELECT osm_id, tags->>'name' AS name
                FROM {leaf_table}
                WHERE static_embedding IS NOT NULL
                ORDER BY static_embedding <=> (
                    SELECT static_embedding FROM {leaf_table}
                    WHERE static_embedding IS NOT NULL LIMIT 1
                )
                LIMIT 3;
            """)
            plan = cursor.fetchall()
            for row in plan:
                if "Index Scan" in row[0] or "Execution Time" in row[0]:
                    self.stdout.write(f"    {row[0].strip()}")

            # 5d. ANN query via materialized view
            self.stdout.write(f"  [5d] ANN query via materialized view {mv_table}:")
            cursor.execute(f"""
                EXPLAIN (ANALYZE, FORMAT text)
                SELECT osm_id, tags->>'name' AS name
                FROM {mv_table}
                WHERE static_embedding IS NOT NULL
                ORDER BY static_embedding <=> (
                    SELECT static_embedding FROM {mv_table}
                    WHERE static_embedding IS NOT NULL LIMIT 1
                )
                LIMIT 3;
            """)
            plan = cursor.fetchall()
            for row in plan:
                if "Index Scan" in row[0] or "Seq Scan" in row[0] or "Execution Time" in row[0]:
                    self.stdout.write(f"    {row[0].strip()}")

            # 5e. Upsert test — INSERT ... ON CONFLICT into the leaf
            self.stdout.write("  [5e] Upsert test (ON CONFLICT into leaf):")
            test_osm_id = 999999999
            cursor.execute(f"""
                INSERT INTO {leaf_table} (
                    id, snapshot_id, country_code, subdivision,
                    osm_type, osm_id, tags, gv_tags_version, gv_nle_trained
                ) VALUES (
                    999999999, '{snapshot}', '{country}', NULL,
                    'node', {test_osm_id}, '{{"test": "true"}}'::jsonb, '1.0', FALSE
                )
                ON CONFLICT (osm_type, osm_id, gv_tags_version, snapshot_id, country_code)
                DO UPDATE SET tags = EXCLUDED.tags, updated_at = NOW()
                RETURNING (CASE WHEN xmax = 0 THEN 'inserted' ELSE 'updated' END) AS op;
            """)
            result = cursor.fetchone()
            self.stdout.write(f"    upsert result: {result[0]}")

            # Update the same row (should be 'updated')
            cursor.execute(f"""
                INSERT INTO {leaf_table} (
                    id, snapshot_id, country_code, subdivision,
                    osm_type, osm_id, tags, gv_tags_version, gv_nle_trained
                ) VALUES (
                    999999999, '{snapshot}', '{country}', NULL,
                    'node', {test_osm_id}, '{{"test": "updated"}}'::jsonb, '1.0', FALSE
                )
                ON CONFLICT (osm_type, osm_id, gv_tags_version, snapshot_id, country_code)
                DO UPDATE SET tags = EXCLUDED.tags, updated_at = NOW()
                RETURNING (CASE WHEN xmax = 0 THEN 'inserted' ELSE 'updated' END) AS op;
            """)
            result = cursor.fetchone()
            self.stdout.write(f"    second upsert: {result[0]}")

            # Clean up the test row
            cursor.execute(f"""
                DELETE FROM {leaf_table} WHERE osm_id = {test_osm_id};
            """)
            self.stdout.write(f"    test row cleaned up")

    # ── Phase 6: PartitionRegistry tracking ───────────────────────────────

    def _phase6_partition_registry(self, snapshot, country, leaf_table):
        self.stdout.write(self.style.MIGRATE_HEADING(
            "[6] PartitionRegistry tracking"
        ))
        from worldkg_nca.models import OsmEntity
        from django.db import connections

        # Count entities in the leaf
        with connections["vectors"].cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {leaf_table};")
            entity_count = cursor.fetchone()[0]
            cursor.execute(f"SELECT count(*) FROM {leaf_table} WHERE static_embedding IS NOT NULL;")
            embedding_count = cursor.fetchone()[0]

        # Create/update the PartitionRegistry row
        obj, created = PartitionRegistry.get_or_create_pending(snapshot, country, None)
        obj.mark_complete(entity_count=entity_count, embedding_count=embedding_count)
        status = "created" if created else "updated"
        self.stdout.write(self.style.SUCCESS(
            f"  PartitionRegistry {status}: {snapshot}/{country}/<default> "
            f"entities={entity_count} embeddings={embedding_count}"
        ))

        # Verify idempotency gate
        is_complete = PartitionRegistry.is_complete(snapshot, country)
        self.stdout.write(f"  is_complete({snapshot}, {country}) = {is_complete}")

    # ── Cleanup ───────────────────────────────────────────────────────────

    def _cleanup(self, snapshot, country):
        self.stdout.write(self.style.WARNING(
            f"Cleaning up physical sharding prototype ({snapshot}/{country})..."
        ))
        cc_lower = country.lower()
        snap_table = SNAPSHOT_TABLE.format(snapshot=snapshot)
        leaf_table = LEAF_TABLE.format(snapshot=snapshot, cc_lower=cc_lower)
        mv_table = MV_TABLE.format(snapshot=snapshot, cc_lower=cc_lower)

        with connections["vectors"].cursor() as cursor:
            # Drop MV first (depends on leaf)
            cursor.execute(f"DROP MATERIALIZED VIEW IF EXISTS {mv_table};")
            self.stdout.write(f"  dropped MV: {mv_table}")

            # Drop snapshot partition (cascades to leaf partitions)
            cursor.execute(f"DROP TABLE IF EXISTS {snap_table} CASCADE;")
            self.stdout.write(f"  dropped snapshot partition: {snap_table}")

            # Drop root if no partitions remain
            cursor.execute(f"""
                SELECT count(*) FROM pg_inherits
                WHERE inhparent = '{ROOT_TABLE}'::regclass;
            """)
            remaining = cursor.fetchone()[0]
            if remaining == 0:
                cursor.execute(f"DROP TABLE IF EXISTS {ROOT_TABLE};")
                self.stdout.write(f"  dropped root: {ROOT_TABLE} (no partitions remain)")
            else:
                self.stdout.write(f"  kept root: {ROOT_TABLE} ({remaining} partitions remain)")

        # Clean up PartitionRegistry test rows
        PartitionRegistry.objects.filter(
            snapshot_id=snapshot, country_code=country, subdivision=None,
        ).delete()
        self.stdout.write(f"  cleaned PartitionRegistry rows for {snapshot}/{country}")

        self.stdout.write(self.style.SUCCESS("Cleanup complete."))
