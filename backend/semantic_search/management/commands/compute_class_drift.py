"""Compute WorldKG class-distribution drift between consecutive snapshots.

ISSUE-class-drift-capability: Step 6 fills the newest pair inline when
a run lands; this command is the backfill path for the rest of the
series. It scans each country's snapshot series, fills any consecutive
pair missing a ``WorldKGClassDrift`` row via
``WorldKGDriftService.compute_drift``, and is idempotent (existing
rows are skipped). Fingerprints are created on demand by the service;
Step 6 also writes one per run.
"""

import logging
from django.core.management.base import BaseCommand

from osmsnapshot.models import Snapshot, WorldKGClassDrift
from semantic_search.services.worldkg_drift_service import (
    get_worldkg_drift_service,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Fill missing WorldKGClassDrift rows for consecutive snapshot "
        "pairs (per country). Idempotent; safe to run repeatedly."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--country",
            type=str,
            default=None,
            help="ISO 3166-1 alpha-2 code to scope (default: all countries)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Report missing pairs without computing",
        )

    def handle(self, *args, **options):
        country = options["country"]
        dry_run = options["dry_run"]

        snapshots = Snapshot.objects.order_by("country_code", "snapshot_date")
        if country:
            snapshots = snapshots.filter(country_code__iexact=country)

        ***REMOVED*** Group each country's snapshots in date order — the CharField
        ***REMOVED*** YYYY_MM_DD key sorts lexicographically.
        by_country = {}
        for snap in snapshots:
            by_country.setdefault(snap.country_code.upper(), []).append(snap)

        service = get_worldkg_drift_service()
        computed = skipped = errors = 0

        for country_code, snaps in sorted(by_country.items()):
            if len(snaps) < 2:
                self.stdout.write(
                    f"{country_code}: {len(snaps)} snapshot(s) — "
                    "no pairs, nothing to do"
                )
                continue
            for snap_from, snap_to in zip(snaps, snaps[1:]):
                label = (
                    f"{country_code} {snap_from.snapshot_date} -> "
                    f"{snap_to.snapshot_date}"
                )
                if dry_run:
                    missing = not WorldKGClassDrift.objects.filter(
                        region=country_code,
                        snapshot_from=snap_from,
                        snapshot_to=snap_to,
                    ).exists()
                    self.stdout.write(
                        f"{label}: "
                        + ("missing (would compute)" if missing
                           else "drift row exists, skipped")
                    )
                    computed += missing
                    skipped += not missing
                    continue
                try:
                    drift = service.fill_pair_if_missing(
                        snap_from, snap_to
                    )
                    if drift is None:
                        skipped += 1
                        self.stdout.write(
                            f"{label}: drift row exists, skipped"
                        )
                        continue
                    computed += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"{label}: JS={drift.js_divergence:.3f} "
                        f"magnitude={drift.drift_magnitude}"
                    ))
                except Exception as exc:
                    errors += 1
                    self.stdout.write(self.style.ERROR(
                        f"{label}: failed — {exc}"
                    ))

        self.stdout.write(self.style.SUCCESS(
            f"\nDone: {computed} computed"
            + (" (dry-run)" if dry_run else "")
            + f", {skipped} existing, {errors} errors"
        ))
