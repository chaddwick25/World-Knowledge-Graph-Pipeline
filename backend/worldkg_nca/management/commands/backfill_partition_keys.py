"""Backfill ``OsmEntity.snapshot_id`` and ``OsmEntity.country_code`` (Phase 6).

Step 0 of ``docs/plans/PHASE6_OSMID_AUDIT_AND_CUTOVER_PLAN.md``.

The partitioned table (``embeddings_partitioned``) needs two partition keys
that the monolith (``semantic_search_osmentity``) does not have:

- ``snapshot_id`` (VARCHAR ``YYYY_MM_DD``) — derived from
  ``source_snapshot_id`` → ``TemporalSnapshot.timestamp``.
- ``country_code`` (ISO 3166-1 alpha-2) — derived via spatial join:
  ``geom && ST_MakeEnvelope(country_bbox)``.

This command populates both fields on existing monolith rows.  It is
non-destructive: it only sets values on rows where the field is NULL, and
does not change the unique constraint.

Usage::

    python manage.py backfill_partition_keys                     # both
    python manage.py backfill_partition_keys --dry-run           # report only
    python manage.py backfill_partition_keys --snapshot-only     # just snapshot_id
    python manage.py backfill_partition_keys --country-only      # just country_code
    python manage.py backfill_partition_keys --force-country-code  # re-run country_code with osm_boundaries
    python manage.py backfill_partition_keys --default-snapshot 2025_12_31
    python manage.py backfill_partition_keys --limit 5000        # cap for testing

The command is idempotent: rows that already have a value are skipped.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import connections, transaction

from worldkg_nca.models import OsmEntity
from worldkg_nca.snapshot_utils import snapshot_id_from_uuid, DEFAULT_SNAPSHOT_ID

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Backfill OsmEntity.snapshot_id and country_code partition keys "
        "for Phase 6 temporal sharding cutover."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Do not write any changes; only report what would happen.",
        )
        parser.add_argument(
            "--snapshot-only",
            action="store_true",
            help="Only backfill snapshot_id (skip country_code).",
        )
        parser.add_argument(
            "--country-only",
            action="store_true",
            help="Only backfill country_code (skip snapshot_id).",
        )
        parser.add_argument(
            "--default-snapshot",
            default=DEFAULT_SNAPSHOT_ID,
            help=(
                "snapshot_id to use for rows with no source_snapshot_id "
                f"(default: {DEFAULT_SNAPSHOT_ID})."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Cap the number of rows processed per phase (for testing).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-resolve and overwrite even for rows that already have a value.",
        )
        parser.add_argument(
            "--force-country-code",
            action="store_true",
            help=(
                "Shortcut for --country-only --force: re-run only the country_code "
                "backfill, overwriting existing values.  Used in Phase 3 of "
                "OSMENTITY_MONOLITH_OPTIMIZATION.md after osm_boundaries data is "
                "available to match the 2.5M NULL rows."
            ),
        )

    # ── Phase 1: snapshot_id ───────────────────────────────────────────

    def _backfill_snapshot_id(self, default_snapshot, dry_run, limit, force):
        """Populate snapshot_id from source_snapshot_id → TemporalSnapshot.

        Rows with a ``source_snapshot_id`` (UUID) are resolved via
        ``TemporalSnapshot.timestamp``.  Rows without one get the default
        snapshot (typically ``'2025_12_31'`` — the gv_tags_version all
        monolith rows share).
        """
        qs_with_uuid = OsmEntity.objects.using("vectors")
        if not force:
            qs_with_uuid = qs_with_uuid.filter(snapshot_id__isnull=True)
        qs_with_uuid = qs_with_uuid.exclude(source_snapshot_id__isnull=True)
        if limit:
            qs_with_uuid = qs_with_uuid[:limit]

        total_with_uuid = qs_with_uuid.count()
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"  [snapshot_id] {total_with_uuid} rows with source_snapshot_id "
            f"to process (force={force})."
        ))

        updated = 0

        if total_with_uuid > 0:
            # Build a lookup of source_snapshot_id → snapshot_id string
            uuids = set(qs_with_uuid.values_list("source_snapshot_id", flat=True).distinct())
            uuid_to_snapshot = {}
            for suid in uuids:
                sid = snapshot_id_from_uuid(suid)
                if sid:
                    uuid_to_snapshot[suid] = sid

            unresolved = uuids - set(uuid_to_snapshot.keys())
            if unresolved:
                self.stdout.write(self.style.WARNING(
                    f"  [snapshot_id] {len(unresolved)} UUIDs could not be resolved "
                    f"— will use default '{default_snapshot}'."
                ))

            # Batch update per snapshot_id value to avoid per-row saves
            for suid, sid in uuid_to_snapshot.items():
                if dry_run:
                    count = qs_with_uuid.filter(source_snapshot_id=suid).count()
                    self.stdout.write(
                        f"  [dry-run] snapshot_id={sid} for {count} rows "
                        f"(source_snapshot_id={suid})"
                    )
                    updated += count
                else:
                    count = qs_with_uuid.filter(source_snapshot_id=suid).update(snapshot_id=sid)
                    updated += count

            # Handle unresolved UUIDs with the default
            if unresolved:
                if dry_run:
                    count = qs_with_uuid.filter(
                        source_snapshot_id__in=list(unresolved)
                    ).count()
                    self.stdout.write(
                        f"  [dry-run] snapshot_id={default_snapshot} (default) "
                        f"for {count} rows (unresolved UUIDs)"
                    )
                    updated += count
                else:
                    count = qs_with_uuid.filter(
                        source_snapshot_id__in=list(unresolved)
                    ).update(snapshot_id=default_snapshot)
                    updated += count

        # Handle rows with no source_snapshot_id at all — use the default.
        # This is the common case on the monolith (all 3M rows have
        # source_snapshot_id IS NULL and gv_tags_version='2025_12_31').
        qs_no_uuid = OsmEntity.objects.using("vectors").filter(
            source_snapshot_id__isnull=True
        )
        if not force:
            qs_no_uuid = qs_no_uuid.filter(snapshot_id__isnull=True)
        if limit:
            qs_no_uuid = qs_no_uuid[:limit]
        count_no_uuid = qs_no_uuid.count()
        if count_no_uuid:
            if dry_run:
                self.stdout.write(
                    f"  [dry-run] snapshot_id={default_snapshot} (default) "
                    f"for {count_no_uuid} rows with no source_snapshot_id"
                )
            else:
                qs_no_uuid.update(snapshot_id=default_snapshot)
            updated += count_no_uuid
        elif total_with_uuid == 0:
            self.stdout.write("  [snapshot_id] nothing to do.")

        self.stdout.write(self.style.SUCCESS(
            f"  [snapshot_id] updated={updated} (dry_run={dry_run})."
        ))
        return updated

    # ── Phase 2: country_code ──────────────────────────────────────────

    def _get_country_bboxes(self):
        """Build {iso2: (min_lon, min_lat, max_lon, max_lat)} from DB.

        Loads bboxes from ALL CountryPipelineProfile rows (not just
        ``has_embeddings=True``) so that entities from countries without
        completed pipeline runs still get a country_code.  Falls back to
        ``resolve_country_bbox`` (which checks OsmBoundary) for profiles
        without a stored bbox in their payload.

        Countries with antimeridian-crossing bboxes (lon span > 180°, e.g.
        US = [-180, 180]) are clamped to their eastern hemisphere portion
        to avoid the bbox covering the entire planet and claiming rows from
        other countries.  The western hemisphere portion (Alaska) is added
        as a separate entry with a ``_W`` suffix so it's still matched.
        """
        from orchestration.models import CountryPipelineProfile
        from extraction.services.osm_wikidata_resolver import resolve_country_bbox

        bboxes = {}
        # Load ALL profiles — the monolith contains entities from the full
        # planet extract, not just countries with completed pipeline runs.
        profiles = CountryPipelineProfile.objects.exclude(
            iso2__isnull=True
        ).exclude(iso2="")

        for profile in profiles.iterator():
            iso2 = profile.iso2.upper()
            # Try the stored payload first
            payload = profile.country_relations_payload or {}
            bb = payload.get("bbox")
            if bb and all(k in bb for k in ("min_lon", "min_lat", "max_lon", "max_lat")):
                bbox = (
                    float(bb["min_lon"]), float(bb["min_lat"]),
                    float(bb["max_lon"]), float(bb["max_lat"]),
                )
            else:
                # Fall back to resolve_country_bbox (checks OsmBoundary DB)
                bbox = resolve_country_bbox(iso2)
            if not bbox:
                continue

            min_lon, min_lat, max_lon, max_lat = bbox
            lon_span = max_lon - min_lon

            # Antimeridian-crossing bbox (lon span > 180°): the bbox wraps
            # around the planet.  For spatial join purposes, ST_MakeEnvelope
            # with [-180, ..., 180, ...] covers the entire planet and would
            # claim every row.  We handle two cases:
            #
            # 1. lon_span >= 350° (essentially the entire planet, e.g. US
            #    with overseas territories): use a tighter continental bbox.
            #    The US continental + Alaska + Hawaii range is approximately
            #    [-180, 18, -65, 72].  This will miss some territories (Guam,
            #    Puerto Rico, US Virgin Islands) but those entities are
            #    better left unmatched than having the US claim the entire
            #    northern hemisphere.
            #
            # 2. 180° < lon_span < 350° (genuine antimeridian crossing, e.g.
            #    Russia [-180, ..., 180, ...] is actually case 1 too):
            #    split into two hemispheres.
            if lon_span >= 350:
                # Use a tighter bbox for countries that span the entire planet.
                # This is a known limitation — overseas territories will be
                # unmatched.  Better than claiming the entire hemisphere.
                if iso2 == "US":
                    tighter = (-180.0, 17.0, -65.0, 72.0)
                elif iso2 == "RU":
                    tighter = (19.0, 41.0, 180.0, 78.0)
                else:
                    # Skip — can't determine a tighter bbox automatically.
                    self.stdout.write(self.style.WARNING(
                        f"  [country_code] {iso2} bbox spans {lon_span:.0f}° "
                        f"— skipping (would match entire planet)."
                    ))
                    continue
                bboxes[iso2] = (iso2, tighter)
            elif lon_span > 180:
                # Genuine antimeridian crossing: split into two hemispheres.
                bboxes[f"{iso2}_E"] = (iso2, (-179.9, min_lat, max_lon, max_lat))
                bboxes[f"{iso2}_W"] = (iso2, (min_lon, min_lat, 179.9, max_lat))
            else:
                bboxes[iso2] = (iso2, bbox)

        return bboxes

    def _backfill_country_code(self, dry_run, limit, force):
        """Populate country_code via spatial join: geom → country bbox.

        When ``force=True``, all existing country_code values are first NULLed
        out, then countries are processed from **smallest bbox area to largest**.
        This ensures small countries (e.g. Cuba, Iceland) get first dibs on
        their rows before large countries (e.g. US with a 360° antimeridian
        bbox) claim them.  Without this ordering, a large country's bbox
        would overwrite rows that belong to smaller countries inside it.
        """
        from django.db.models import Q

        qs = OsmEntity.objects.using("vectors")
        if not force:
            qs = qs.filter(country_code__isnull=True)
        qs = qs.filter(geom__isnull=False)
        if limit:
            qs = qs[:limit]

        total = qs.count()
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"  [country_code] {total} rows with geom to process (force={force})."
        ))
        if total == 0:
            self.stdout.write("  [country_code] nothing to do.")
            return 0

        bboxes = self._get_country_bboxes()
        self.stdout.write(
            f"  [country_code] resolved {len(bboxes)} country bboxes from DB."
        )
        if not bboxes:
            self.stdout.write(self.style.ERROR(
                "  [country_code] no bboxes available — cannot proceed."
            ))
            return 0

        # ── Sort by bbox area (smallest first) ──────────────────────────────
        # Small countries must be processed before large ones so they claim
        # their rows first.  Without this, the US bbox (360° span from
        # antimeridian crossing) would overwrite Cuba, Iceland, etc.
        # Each value is (real_iso2, bbox) — the key may be a split suffix.
        sorted_bboxes = sorted(
            bboxes.items(),
            key=lambda kv: (kv[1][1][2] - kv[1][1][0]) * (kv[1][1][3] - kv[1][1][1]),
        )

        # ── Force mode: NULL out existing country_codes ─────────────────────
        # Only touch rows that HAVE a country_code (~444K), not all 3M rows.
        # The previous approach (WHERE geom IS NOT NULL) updated all 3M rows
        # in a single transaction, causing OOM crashes and deadlocks.
        if force and not dry_run:
            with connections["vectors"].cursor() as cursor:
                cursor.execute(
                    "UPDATE semantic_search_osmentity SET country_code = NULL "
                    "WHERE country_code IS NOT NULL"
                )
                nulled = cursor.rowcount
                self.stdout.write(
                    f"  [country_code] force mode: NULLed {nulled} existing "
                    f"country_code values before re-backfill."
                )

        updated = 0
        unmatched = 0

        # Use raw SQL for the spatial join (much faster than ORM per-row).
        # Always use "country_code IS NULL" in the WHERE clause — even in
        # force mode — because we NULLed everything above.  This prevents
        # larger countries from overwriting smaller countries' matches.
        # NOTE: We don't set updated_at here to avoid conflicts with autovacuum
        # (the updated_at column triggers tuple rechecks under concurrent VACUUM).
        with connections["vectors"].cursor() as cursor:
            for _key, (iso2, (min_lon, min_lat, max_lon, max_lat)) in sorted_bboxes:
                where_clause = "country_code IS NULL"
                if limit:
                    # PostgreSQL doesn't support LIMIT in UPDATE directly.
                    # Use ctid subquery to limit the number of rows updated.
                    sql = f"""
                        UPDATE semantic_search_osmentity
                        SET country_code = %s
                        WHERE ctid IN (
                            SELECT ctid FROM semantic_search_osmentity
                            WHERE {where_clause}
                              AND geom IS NOT NULL
                              AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                            LIMIT %s
                        )
                    """
                    params = [iso2, min_lon, min_lat, max_lon, max_lat, limit]
                else:
                    sql = f"""
                        UPDATE semantic_search_osmentity
                        SET country_code = %s
                        WHERE {where_clause}
                          AND geom IS NOT NULL
                          AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                    """
                    params = [iso2, min_lon, min_lat, max_lon, max_lat]

                if dry_run:
                    # Count matching rows instead of updating
                    count_sql = f"""
                        SELECT count(*) FROM semantic_search_osmentity
                        WHERE {where_clause}
                          AND geom IS NOT NULL
                          AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                    """
                    cursor.execute(count_sql, [min_lon, min_lat, max_lon, max_lat])
                    count = cursor.fetchone()[0]
                    if count:
                        self.stdout.write(
                            f"  [dry-run] country_code={iso2} for {count} rows"
                        )
                        updated += count
                else:
                    cursor.execute(sql, params)
                    count = cursor.rowcount
                    if count:
                        self.stdout.write(
                            f"  [country_code] set {iso2} for {count} rows"
                        )
                        updated += count

        # Count remaining unmatched rows
        if not dry_run:
            with connections["vectors"].cursor() as cursor:
                cursor.execute("""
                    SELECT count(*) FROM semantic_search_osmentity
                    WHERE country_code IS NULL AND geom IS NOT NULL
                """)
                unmatched = cursor.fetchone()[0]
        else:
            # In dry-run, estimate unmatched as total - updated
            unmatched = max(0, total - updated)

        if unmatched:
            self.stdout.write(self.style.WARNING(
                f"  [country_code] {unmatched} rows with geom did not match "
                f"any country bbox (may be in international waters or "
                f"unsupported countries)."
            ))

        self.stdout.write(self.style.SUCCESS(
            f"  [country_code] updated={updated} (dry_run={dry_run}), "
            f"unmatched={unmatched}."
        ))
        return updated

    # ── Main ───────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        snapshot_only = options["snapshot_only"]
        country_only = options["country_only"]
        default_snapshot = options["default_snapshot"]
        limit = options.get("limit")
        force = options["force"]
        force_country_code = options["force_country_code"]

        # --force-country-code is a shortcut for --country-only --force
        if force_country_code:
            country_only = True
            force = True

        # If neither flag is set, do both
        do_snapshot = not country_only
        do_country = not snapshot_only

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Backfilling partition keys on OsmEntity "
            f"(dry_run={dry_run}, limit={limit}, force={force})."
        ))

        total_updated = 0
        if do_snapshot:
            total_updated += self._backfill_snapshot_id(
                default_snapshot, dry_run, limit, force
            )
        if do_country:
            total_updated += self._backfill_country_code(
                dry_run, limit, force
            )

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Total rows updated: {total_updated} (dry_run={dry_run})."
        ))

        # Report current state
        with connections["vectors"].cursor() as cursor:
            cursor.execute("""
                SELECT
                    count(*) AS total,
                    count(*) FILTER (WHERE snapshot_id IS NOT NULL) AS with_snapshot,
                    count(*) FILTER (WHERE country_code IS NOT NULL) AS with_country,
                    count(*) FILTER (WHERE snapshot_id IS NOT NULL AND country_code IS NOT NULL) AS with_both
                FROM semantic_search_osmentity
            """)
            row = cursor.fetchone()
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\nCurrent state:\n"
                f"  total rows:       {row[0]}\n"
                f"  with snapshot_id: {row[1]} ({row[1] * 100 // row[0] if row[0] else 0}%)\n"
                f"  with country_code:{row[2]} ({row[2] * 100 // row[0] if row[0] else 0}%)\n"
                f"  with both:        {row[3]} ({row[3] * 100 // row[0] if row[0] else 0}%)"
            ))
