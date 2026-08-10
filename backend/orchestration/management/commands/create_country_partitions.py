"""Create country leaf partitions + materialized views for all completed SnapshotJobs.

Implements Steps 1-2 of ``docs/plans/PHASE6_COMPLETION_PLAN.md``.

This command creates the partitioned table hierarchy for ``semantic_search_osmentity``
(the Django OsmEntity model's table).  For each ``(snapshot_date, country_code)``
pair it:

  1. Ensures the root partitioned table (``semantic_search_osmentity``) exists
  2. Creates the snapshot sub-partition (``embeddings_{snapshot}``)
  3. Creates the country leaf partition (``embeddings_{snapshot}_{cc_lower}``)
  4. Migrates data from the monolith (if any) via spatial filter by country bbox
  5. Builds a HNSW index on ``gv_tags_embedding`` on the leaf
  6. Creates a materialized view (``mv_embeddings_{snapshot}_{cc_lower}``)
  7. Records completion in ``PartitionRegistry``

**Fresh DB approach:** Django's migration creates ``semantic_search_osmentity``
as a regular (non-partitioned) table.  On first run, this command detects the
empty non-partitioned table, drops it, and recreates as a partitioned table.
All ORM queries work against the partitioned table transparently.

Bboxes are resolved from the DB (``CountryPipelineProfile.country_relations_payload``
or ``OsmBoundary``) — not from a hardcoded dict.  This covers all 191 countries.

Usage::

    # All completed SnapshotJobs (default)
    python manage.py create_country_partitions

    # Specific country + snapshot
    python manage.py create_country_partitions --country CU --snapshot 2025_12_31

    # Structure only (no data migration, no HNSW, no MV)
    python manage.py create_country_partitions --skip-data

    # MV only — create/refresh materialized view for a country (used by step_6)
    python manage.py create_country_partitions --country CU --mv-only

    # Dry run — report what would happen
    python manage.py create_country_partitions --dry-run

    # Cleanup a specific country's partition + MV
    python manage.py create_country_partitions --cleanup --country CU

The command is idempotent: re-running it skips partitions that are already
complete (checked via ``PartitionRegistry.is_complete``).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from django.core.management.base import BaseCommand
from django.db import connections

from orchestration.models import PartitionRegistry, SnapshotJob

logger = logging.getLogger(__name__)

# The root partitioned table IS the Django model's table.
# Django's migration creates it as a regular table; _ensure_root_table()
# converts it to a partitioned table on first run.
ROOT_TABLE = "semantic_search_osmentity"
SNAPSHOT_TABLE = "embeddings_{snapshot}"          # e.g. embeddings_2025_12_31
LEAF_TABLE = "embeddings_{snapshot}_{cc_lower}"   # e.g. embeddings_2025_12_31_cu
MV_TABLE = "mv_embeddings_{snapshot}_{cc_lower}"  # e.g. mv_embeddings_2025_12_31_cu


class Command(BaseCommand):
    help = (
        "Create country leaf partitions + materialized views for all "
        "completed SnapshotJobs (Phase 6 Steps 1-2)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--country", default=None,
            help="ISO 3166-1 alpha-2 country code. If omitted, process all "
                 "completed SnapshotJobs.",
        )
        parser.add_argument(
            "--snapshot", default=None,
            help="Snapshot id (YYYY_MM_DD). If omitted, use snapshot_date "
                 "from each SnapshotJob record.",
        )
        parser.add_argument(
            "--cleanup", action="store_true",
            help="Drop the leaf partition + MV for the given --country/--snapshot, "
                 "then exit.",
        )
        parser.add_argument(
            "--skip-data", action="store_true",
            help="Create partition structure but skip data migration.",
        )
        parser.add_argument(
            "--skip-hnsw", action="store_true",
            help="Skip HNSW index build on the leaf.",
        )
        parser.add_argument(
            "--skip-mv", action="store_true",
            help="Skip materialized view creation.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would happen without making changes.",
        )
        parser.add_argument(
            "--hnsw-m", type=int, default=16,
            help="HNSW M parameter (default: 16).",
        )
        parser.add_argument(
            "--hnsw-ef-construction", type=int, default=128,
            help="HNSW ef_construction (default: 128).",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Re-process even if PartitionRegistry says the partition is "
                 "complete.  Re-migrates data (ON CONFLICT upsert).",
        )
        parser.add_argument(
            "--mv-only", action="store_true",
            help="Only create/refresh the materialized view for the given "
                 "country/snapshot.  Skip partition creation + data migration. "
                 "Used by step_6 after GV-NLE training is complete.",
        )

    # ── Main ─────────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        country = options["country"].upper() if options["country"] else None
        snapshot = options["snapshot"]
        cleanup = options["cleanup"]
        skip_data = options["skip_data"]
        skip_hnsw = options["skip_hnsw"]
        skip_mv = options["skip_mv"]
        dry_run = options["dry_run"]
        m = options["hnsw_m"]
        ef_construction = options["hnsw_ef_construction"]
        force = options["force"]
        mv_only = options["mv_only"]

        if cleanup:
            if not country:
                self.stdout.write(self.style.ERROR(
                    "--cleanup requires --country."
                ))
                return
            self._cleanup(snapshot or "2025_12_31", country)
            return

        if mv_only:
            if not country:
                self.stdout.write(self.style.ERROR(
                    "--mv-only requires --country."
                ))
                return
            snap = snapshot or "2025_12_31"
            cc_lower = country.lower()
            leaf_table = LEAF_TABLE.format(snapshot=snap, cc_lower=cc_lower)
            mv_table = MV_TABLE.format(snapshot=snap, cc_lower=cc_lower)
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"  [{snap}/{country}] MV-only: {mv_table}"
            ))
            if not dry_run:
                with connections["vectors"].cursor() as cursor:
                    self._create_mview(cursor, leaf_table, mv_table, country, snap,
                                       skip_hnsw)
            self.stdout.write(self.style.SUCCESS(
                f"  [{snap}/{country}] MV created/refreshed."
            ))
            return

        # Build the list of (snapshot, country) pairs to process
        targets = self._resolve_targets(country, snapshot)
        if not targets:
            self.stdout.write(self.style.WARNING(
                "No completed SnapshotJob records found. "
                "Nothing to do."
            ))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Creating country partitions for {len(targets)} target(s):\n"
            + "\n".join(f"  {s}/{c}" for s, c in targets)
        ))
        self.stdout.write("-" * 72)

        # Resolve bboxes for all target countries
        bboxes = self._resolve_bboxes([c for _, c in targets])
        if not bboxes:
            self.stdout.write(self.style.ERROR(
                "No country bboxes could be resolved. "
                "Ensure CountryPipelineProfile / OsmBoundary data is populated."
            ))
            return

        # Ensure the root table exists (once per run)
        if not dry_run:
            with connections["vectors"].cursor() as cursor:
                self._ensure_root_table(cursor)

        succeeded = 0
        failed = 0
        skipped = 0

        for snap, cc in targets:
            if cc not in bboxes:
                self.stdout.write(self.style.WARNING(
                    f"  [{snap}/{cc}] no bbox resolved — skipping."
                ))
                skipped += 1
                continue

            # Idempotency gate
            if not force and PartitionRegistry.is_complete(snap, cc):
                self.stdout.write(
                    f"  [{snap}/{cc}] already complete (PartitionRegistry) — skipping."
                )
                skipped += 1
                continue

            try:
                self._process_country(
                    snap, cc, bboxes[cc],
                    skip_data=skip_data,
                    skip_hnsw=skip_hnsw,
                    skip_mv=skip_mv,
                    dry_run=dry_run,
                    hnsw_m=m,
                    hnsw_ef=ef_construction,
                    force=force,
                )
                succeeded += 1
            except Exception as exc:
                self.stdout.write(self.style.ERROR(
                    f"  [{snap}/{cc}] FAILED: {exc}"
                ))
                logger.exception("create_country_partitions failed for %s/%s", snap, cc)
                failed += 1

        self.stdout.write("-" * 72)
        self.stdout.write(self.style.SUCCESS(
            f"Done. succeeded={succeeded} skipped={skipped} failed={failed} "
            f"(dry_run={dry_run})."
        ))

    # ── Target resolution ────────────────────────────────────────────────

    def _resolve_targets(
        self, country: Optional[str], snapshot: Optional[str]
    ) -> List[Tuple[str, str]]:
        """Build list of (snapshot_date, country_code) pairs to process.

        If ``country`` is given, process just that country (with the given
        snapshot or the latest completed one).  Otherwise, query all
        COMPLETED ``SnapshotJob`` records.
        """
        if country:
            if snapshot:
                return [(snapshot, country)]
            # Find the latest completed SnapshotJob for this country
            job = (
                SnapshotJob.objects
                .filter(country_code=country, status=SnapshotJob.Status.COMPLETED)
                .order_by("-snapshot_date")
                .first()
            )
            if job:
                return [(job.snapshot_date, job.country_code)]
            self.stdout.write(self.style.WARNING(
                f"No completed SnapshotJob for {country}. "
                f"Use --snapshot to specify one explicitly."
            ))
            return []

        # All completed SnapshotJobs
        jobs = (
            SnapshotJob.objects
            .filter(status=SnapshotJob.Status.COMPLETED)
            .order_by("snapshot_date", "country_code")
        )
        if snapshot:
            jobs = jobs.filter(snapshot_date=snapshot)

        return [(j.snapshot_date, j.country_code) for j in jobs]

    # ── Bbox resolution ──────────────────────────────────────────────────

    def _resolve_bboxes(self, country_codes: List[str]) -> Dict[str, Tuple]:
        """Resolve bboxes for the given country codes from DB.

        Reuses the same resolution logic as ``backfill_partition_keys``:
        ``CountryPipelineProfile.country_relations_payload["bbox"]`` first,
        then ``resolve_country_bbox`` (which checks ``OsmBoundary``).

        Returns ``{iso2: (min_lon, min_lat, max_lon, max_lat)}``.
        """
        from orchestration.models import CountryPipelineProfile
        from extraction.services.osm_wikidata_resolver import resolve_country_bbox

        bboxes: Dict[str, Tuple] = {}
        unique_codes = sorted(set(c.upper() for c in country_codes))

        # Batch-load CountryPipelineProfile payloads
        profiles = {
            p.iso2.upper(): p
            for p in CountryPipelineProfile.objects.filter(
                iso2__in=unique_codes
            ).exclude(iso2__isnull=True).exclude(iso2="")
        }

        for code in unique_codes:
            bbox = None
            profile = profiles.get(code)
            if profile:
                payload = profile.country_relations_payload or {}
                bb = payload.get("bbox")
                if bb and all(k in bb for k in ("min_lon", "min_lat", "max_lon", "max_lat")):
                    bbox = (
                        float(bb["min_lon"]), float(bb["min_lat"]),
                        float(bb["max_lon"]), float(bb["max_lat"]),
                    )
            if not bbox:
                bbox = resolve_country_bbox(code)
            if bbox:
                # Clamp antimeridian-crossing bboxes (lon span > 180)
                min_lon, min_lat, max_lon, max_lat = bbox
                if max_lon - min_lon > 180:
                    if code == "US":
                        bbox = (-180.0, 17.0, -65.0, 72.0)
                    elif code == "RU":
                        bbox = (19.0, 41.0, 180.0, 78.0)
                    else:
                        self.stdout.write(self.style.WARNING(
                            f"  [bbox] {code} spans >180° longitude — "
                            f"using eastern hemisphere only."
                        ))
                        bbox = (-179.9, min_lat, max_lon, max_lat)
                bboxes[code] = bbox
            else:
                self.stdout.write(self.style.WARNING(
                    f"  [bbox] {code} — no bbox resolved."
                ))

        return bboxes

    # ── Root table ───────────────────────────────────────────────────────

    def _ensure_root_table(self, cursor):
        """Ensure the root partitioned table exists.

        On a fresh DB, Django's migration creates ``semantic_search_osmentity``
        as a regular (non-partitioned) table.  This method detects that state,
        verifies the table is empty, drops it, and recreates as a partitioned
        table.  If the table already exists as partitioned, this is a no-op.

        The root is partitioned by ``LIST(snapshot_id)`` with a composite PK
        that includes the partition key (Postgres requirement).
        """
        # Check if the table exists and whether it's already partitioned
        cursor.execute("""
            SELECT c.relname, p.partrelid IS NOT NULL as is_partitioned
            FROM pg_class c
            LEFT JOIN pg_partitioned_table p ON c.oid = p.partrelid
            WHERE c.relname = %s AND c.relkind = 'r';
        """, [ROOT_TABLE])
        row = cursor.fetchone()

        if row and row[1]:
            # Already partitioned — nothing to do
            return

        if row and not row[1]:
            # Table exists but is NOT partitioned — check if empty
            cursor.execute(f"SELECT count(*) FROM {ROOT_TABLE};")
            count = cursor.fetchone()[0]
            if count > 0:
                raise RuntimeError(
                    f"{ROOT_TABLE} exists as a non-partitioned table with "
                    f"{count} rows.  Cannot convert to partitioned table. "
                    f"Back up data, drop the table, and re-run."
                )
            # Empty monolith — safe to drop and recreate as partitioned
            self.stdout.write(
                f"  [root] {ROOT_TABLE} exists as non-partitioned (empty) — "
                f"converting to partitioned table."
            )
            cursor.execute(f"DROP TABLE {ROOT_TABLE} CASCADE;")

        # Create the root partitioned table
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
        # Create a sequence for the id column (BigAutoField replacement).
        # Partitioned tables can't use SERIAL directly, so we create an
        # explicit sequence and set it as the default.
        cursor.execute(f"""
            CREATE SEQUENCE IF NOT EXISTS {ROOT_TABLE}_id_seq;
        """)
        cursor.execute(f"""
            ALTER SEQUENCE {ROOT_TABLE}_id_seq OWNED BY {ROOT_TABLE}.id;
            ALTER TABLE {ROOT_TABLE} ALTER COLUMN id SET DEFAULT nextval('{ROOT_TABLE}_id_seq');
        """)
        # Unique index matching the partitioned table's constraint.
        cursor.execute(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_{ROOT_TABLE}_entity_unique
            ON {ROOT_TABLE} (osm_type, osm_id, gv_tags_version, snapshot_id, country_code);
        """)

    # ── Per-country processing ───────────────────────────────────────────

    def _process_country(
        self, snapshot, country, bbox,
        skip_data, skip_hnsw, skip_mv, dry_run,
        hnsw_m, hnsw_ef, force,
    ):
        cc_lower = country.lower()
        snap_table = SNAPSHOT_TABLE.format(snapshot=snapshot)
        leaf_table = LEAF_TABLE.format(snapshot=snapshot, cc_lower=cc_lower)
        mv_table = MV_TABLE.format(snapshot=snapshot, cc_lower=cc_lower)

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"  [{snapshot}/{country}] leaf={leaf_table}"
        ))

        if dry_run:
            self._dry_run_report(snapshot, country, bbox, leaf_table, mv_table,
                                 skip_data, skip_hnsw, skip_mv)
            return

        with connections["vectors"].cursor() as cursor:
            # 1. Create partition hierarchy
            self._create_partitions(cursor, snapshot, country, snap_table, leaf_table)

            # 2. Migrate data
            if not skip_data:
                if force:
                    # TRUNCATE the leaf before re-migrating to remove any
                    # stale rows from a previous (incorrect) run.
                    cursor.execute(f"TRUNCATE TABLE {leaf_table};")
                    self.stdout.write(f"    [data] truncated {leaf_table} (--force)")
                self._migrate_data(cursor, country, bbox, snapshot, leaf_table, force)
            else:
                self.stdout.write(f"    [data] skipped (--skip-data)")

            # 3. Build HNSW index (independent of data migration —
            # CREATE INDEX IF NOT EXISTS is idempotent, so this is safe
            # even if data wasn't re-migrated).
            if not skip_hnsw:
                self._build_hnsw(cursor, leaf_table, hnsw_m, hnsw_ef)
            else:
                self.stdout.write(f"    [hnsw] skipped (--skip-hnsw)")

            # 4. Create materialized view
            if not skip_mv:
                self._create_mview(cursor, leaf_table, mv_table, country, snapshot,
                                   skip_hnsw)
            else:
                self.stdout.write(f"    [mv] skipped (--skip-mv)")

        # 5. PartitionRegistry tracking
        if not skip_data:
            self._register_partition(snapshot, country, leaf_table)

        self.stdout.write(self.style.SUCCESS(
            f"    [{snapshot}/{country}] OK"
        ))

    # ── Step 1: Create partitions ────────────────────────────────────────

    def _create_partitions(self, cursor, snapshot, country, snap_table, leaf_table):
        """Create snapshot sub-partition + country leaf partition.

        The root table is already ensured by ``_ensure_root_table``.
        """
        # Snapshot partition: sub-partitioned by LIST(country_code)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {snap_table}
            PARTITION OF {ROOT_TABLE}
            FOR VALUES IN ('{snapshot}')
            PARTITION BY LIST (country_code);
        """)

        # Default partition for unmatched country codes in this snapshot
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {snap_table}_default
            PARTITION OF {snap_table} DEFAULT;
        """)

        # Country leaf
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {leaf_table}
            PARTITION OF {snap_table}
            FOR VALUES IN ('{country}');
        """)

        # B-tree + GiST indexes on the leaf
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{leaf_table}_osm ON {leaf_table} (osm_type, osm_id);")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{leaf_table}_geom ON {leaf_table} USING gist (geom);")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{leaf_table}_wkg_class ON {leaf_table} (wkg_class);")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{leaf_table}_wikidata ON {leaf_table} (wikidata_uri);")

    # ── Step 2: Migrate data ─────────────────────────────────────────────

    def _migrate_data(self, cursor, country, bbox, snapshot, leaf_table, force):
        """Bulk INSERT ... SELECT from the root table into the leaf.

        On a fresh DB with the partitioned table, data is upserted directly
        into leaves by the pipeline — this method is only needed when
        migrating data from a previous non-partitioned monolith that was
        converted to the partitioned root.

        Uses ``ON CONFLICT`` for idempotency (re-runnable).  Filters by
        ``snapshot_id`` AND ``country_code`` (the partition keys).
        """
        # Check if there's data in the root table for this snapshot/country
        # that hasn't been routed to a leaf yet (shouldn't happen with
        # partition routing, but handle it for safety)
        cursor.execute(f"""
            SELECT count(*) FROM ONLY {ROOT_TABLE}
            WHERE snapshot_id = '{snapshot}' AND country_code = '{country}';
        """)
        key_count = cursor.fetchone()[0]

        if key_count > 0:
            where_clause = (
                f"WHERE e.snapshot_id = '{snapshot}' "
                f"AND e.country_code = '{country}'"
            )
            self.stdout.write(f"    [data] filtering by partition keys (snapshot_id + country_code)")
        else:
            self.stdout.write(self.style.WARNING(
                f"    [data] no un-partitioned data found — leaf will be "
                f"populated by pipeline upserts."
            ))
            return

        # Count source entities
        cursor.execute(f"SELECT count(*) FROM ONLY {ROOT_TABLE} e {where_clause};")
        count = cursor.fetchone()[0]
        self.stdout.write(f"    [data] source entities: {count}")
        if count == 0:
            self.stdout.write(self.style.WARNING(
                f"    [data] no entities to migrate — empty leaf."
            ))
            return

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
                e.id, e.snapshot_id, e.country_code, NULL,
                e.osm_type, e.osm_id, e.tags,
                e.gv_tags_embedding, e.gv_nle_embedding, e.static_embedding,
                e.wkg_class, e.wkg_superclasses, e.wikidata_uri, e.wkg_depth,
                e.wkg_type_key, e.wkg_type_value, e.wkg_enriched_at,
                e.geom, e.version, e.timestamp,
                e.gv_tags_version, e.gv_nle_version, e.gv_nle_trained,
                e.source_snapshot_id, e.created_at, e.updated_at
            FROM ONLY {ROOT_TABLE} e
            {where_clause}
            ON CONFLICT (osm_type, osm_id, gv_tags_version, snapshot_id, country_code)
            DO UPDATE SET
                tags = EXCLUDED.tags,
                gv_tags_embedding = EXCLUDED.gv_tags_embedding,
                gv_nle_embedding = EXCLUDED.gv_nle_embedding,
                static_embedding = EXCLUDED.static_embedding,
                geom = EXCLUDED.geom,
                updated_at = NOW();
        """)

        cursor.execute(f"SELECT count(*) FROM {leaf_table};")
        migrated = cursor.fetchone()[0]
        self.stdout.write(f"    [data] migrated: {migrated} rows into {leaf_table}")

        cursor.execute(f"""
            SELECT count(*) FROM {leaf_table} WHERE gv_tags_embedding IS NOT NULL;
        """)
        with_tags = cursor.fetchone()[0]
        self.stdout.write(f"    [data] with gv_tags_embedding: {with_tags}/{migrated}")

    # ── Step 3: Build HNSW index ─────────────────────────────────────────

    def _build_hnsw(self, cursor, leaf_table, m, ef_construction):
        """Build HNSW index on ``gv_tags_embedding`` on the leaf.

        The search endpoints (``semantic_search/views_pbf.py``,
        ``worldkg_nca/views.py``) query ``gv_tags_embedding <=>`` for ANN.
        ``static_embedding`` is a separate 400D column used only by the
        optional ``use_ann`` path (default off) — it's empty in the
        monolith, so we index the column that actually has data.

        If an old HNSW index on ``static_embedding`` exists (from a
        previous run), it's dropped first.
        """
        idx_name = f"idx_{leaf_table}_hnsw"
        # Check if the existing index is on the wrong column
        cursor.execute(f"""
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid
                AND a.attnum = ANY(i.indkey)
            JOIN pg_class c ON c.oid = i.indexrelid
            WHERE c.relname = '{idx_name}'
            LIMIT 1;
        """)
        row = cursor.fetchone()
        if row and row[0] != 'gv_tags_embedding':
            self.stdout.write(
                f"    [hnsw] dropping old index on {row[0]} — recreating on gv_tags_embedding"
            )
            cursor.execute(f"DROP INDEX IF EXISTS {idx_name};")

        cursor.execute("SET max_parallel_maintenance_workers = 1;")
        cursor.execute(f"""
            CREATE INDEX IF NOT EXISTS {idx_name}
            ON {leaf_table}
            USING hnsw (gv_tags_embedding vector_cosine_ops)
            WITH (m = {m}, ef_construction = {ef_construction});
        """)
        cursor.execute(f"SELECT pg_size_pretty(pg_relation_size('{idx_name}'));")
        size = cursor.fetchone()[0]
        self.stdout.write(f"    [hnsw] {idx_name} on gv_tags_embedding (size={size})")

    # ── Step 4: Create materialized view ─────────────────────────────────

    def _create_mview(self, cursor, leaf_table, mv_table, country, snapshot,
                      skip_hnsw):
        """Create materialized view over the leaf with its own HNSW index.

        For single-leaf countries the MV is ``SELECT * FROM leaf``.  For
        multi-leaf countries (subdivisions) this would be ``UNION ALL`` of
        all subdivision leaves — not needed today but the pattern is ready.
        """
        cursor.execute(f"DROP MATERIALIZED VIEW IF EXISTS {mv_table};")
        cursor.execute(f"""
            CREATE MATERIALIZED VIEW {mv_table} AS
            SELECT * FROM {leaf_table};
        """)
        # Unique index required for REFRESH CONCURRENTLY
        cursor.execute(f"""
            CREATE UNIQUE INDEX idx_{mv_table}_id
            ON {mv_table} (id);
        """)
        if not skip_hnsw:
            cursor.execute("SET max_parallel_maintenance_workers = 1;")
            cursor.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{mv_table}_hnsw
                ON {mv_table}
                USING hnsw (gv_tags_embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 128);
            """)
            cursor.execute(
                f"SELECT pg_size_pretty(pg_relation_size('idx_{mv_table}_hnsw'));"
            )
            mv_hnsw_size = cursor.fetchone()[0]
            self.stdout.write(f"    [mv] {mv_table} HNSW index (size={mv_hnsw_size})")
        cursor.execute(f"SELECT count(*) FROM {mv_table};")
        count = cursor.fetchone()[0]
        self.stdout.write(f"    [mv] {mv_table} ({count} rows)")

    # ── Step 5: PartitionRegistry ────────────────────────────────────────

    def _register_partition(self, snapshot, country, leaf_table):
        """Record completion in PartitionRegistry."""
        with connections["vectors"].cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {leaf_table};")
            entity_count = cursor.fetchone()[0]
            cursor.execute(
                f"SELECT count(*) FROM {leaf_table} WHERE gv_tags_embedding IS NOT NULL;"
            )
            embedding_count = cursor.fetchone()[0]

        obj, created = PartitionRegistry.get_or_create_pending(snapshot, country, None)
        obj.mark_complete(entity_count=entity_count, embedding_count=embedding_count)
        status = "created" if created else "updated"
        self.stdout.write(
            f"    [registry] {status}: {snapshot}/{country} "
            f"entities={entity_count} embeddings={embedding_count}"
        )

    # ── Dry run ──────────────────────────────────────────────────────────

    def _dry_run_report(self, snapshot, country, bbox, leaf_table, mv_table,
                        skip_data, skip_hnsw, skip_mv):
        self.stdout.write(f"    [dry-run] snapshot={snapshot} country={country}")
        self.stdout.write(f"    [dry-run] bbox={bbox}")
        self.stdout.write(f"    [dry-run] leaf={leaf_table}")
        if not skip_data:
            with connections["vectors"].cursor() as cursor:
                # Check for un-partitioned data in the root table
                cursor.execute(f"""
                    SELECT count(*) FROM ONLY {ROOT_TABLE}
                    WHERE snapshot_id = '{snapshot}' AND country_code = '{country}';
                """)
                count = cursor.fetchone()[0]
                if count > 0:
                    self.stdout.write(f"    [dry-run] entities to migrate (by partition keys): {count}")
                else:
                    self.stdout.write(f"    [dry-run] no un-partitioned data — leaf populated by pipeline upserts")
        if not skip_hnsw:
            self.stdout.write(f"    [dry-run] would build HNSW on gv_tags_embedding for {leaf_table}")
        if not skip_mv:
            self.stdout.write(f"    [dry-run] would create MV: {mv_table}")

    # ── Cleanup ──────────────────────────────────────────────────────────

    def _cleanup(self, snapshot, country):
        """Drop the leaf partition + MV for a specific country/snapshot."""
        cc_lower = country.lower()
        snap_table = SNAPSHOT_TABLE.format(snapshot=snapshot)
        leaf_table = LEAF_TABLE.format(snapshot=snapshot, cc_lower=cc_lower)
        mv_table = MV_TABLE.format(snapshot=snapshot, cc_lower=cc_lower)

        self.stdout.write(self.style.WARNING(
            f"Cleaning up {snapshot}/{country}..."
        ))

        with connections["vectors"].cursor() as cursor:
            cursor.execute(f"DROP MATERIALIZED VIEW IF EXISTS {mv_table};")
            self.stdout.write(f"  dropped MV: {mv_table}")

            # Detach the leaf from the snapshot partition, then drop it.
            # DROP TABLE on a partition also works but may require CASCADE.
            cursor.execute(f"DROP TABLE IF EXISTS {leaf_table} CASCADE;")
            self.stdout.write(f"  dropped leaf: {leaf_table}")

            # Mark PartitionRegistry as failed (cleanup)
            PartitionRegistry.objects.filter(
                snapshot_id=snapshot, country_code=country
            ).update(status=PartitionRegistry.PartitionStatus.FAILED,
                     error_message="cleaned up via create_country_partitions --cleanup")

        self.stdout.write(self.style.SUCCESS(
            f"Cleanup complete for {snapshot}/{country}."
        ))
