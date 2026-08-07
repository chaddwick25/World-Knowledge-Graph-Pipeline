"""Backfill ``PipelineAsset.continent`` from the country -> continent mapping.

Phase 6 of ``docs/plans/TEMPORAL_SHARDING_ARTIFACT_PLAN.md``:

    Backfill ``continent`` on existing ``PipelineAsset`` rows (one-time
    script: ``PipelineRun.country_code`` -> ``CountryPipelineProfile`` ->
    ``continent``).

This is the data-migration step that populates the denormalized ``continent``
field added in migration 0003. Once the field is populated, ``ShardRouter``
can route ``PipelineAsset`` writes to the correct continent shard DB without
a JOIN to ``PipelineRun``.

Usage:

    python manage.py backfill_asset_continents            # write
    python manage.py backfill_asset_continents --dry-run   # report only
    python manage.py backfill_asset_continents --limit 100 # cap for testing

The command is idempotent: rows that already have a ``continent`` are
skipped unless ``--force`` is passed (which re-resolves and overwrites).
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from orchestration.models import CountryPipelineProfile, PipelineAsset, PipelineRun


class Command(BaseCommand):
    help = (
        "Backfill PipelineAsset.continent from "
        "PipelineRun.country_code -> CountryPipelineProfile -> continent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Do not write any changes; only report what would happen.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Cap the number of assets processed (for testing).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-resolve and overwrite even for assets that already have a continent.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options.get("limit")
        force = options["force"]

        qs = PipelineAsset.objects.select_related("pipeline_run").all()
        if not force:
            qs = qs.filter(continent__isnull=True)
        if limit is not None:
            qs = qs[: int(limit)]

        total = qs.count()
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Backfilling continent for {total} PipelineAsset rows"
            f" (dry_run={dry_run}, force={force}, limit={limit})."
        ))

        # Pre-load all CountryPipelineProfile continent mappings to avoid
        # one query per asset. iso2 and iso3 are both indexed.
        profile_by_iso = {}
        for profile in CountryPipelineProfile.objects.all().iterator():
            payload = profile.country_relations_payload or {}
            continent = payload.get("continent") or profile.continent_name or ""
            if profile.iso2:
                profile_by_iso[profile.iso2] = continent
            if profile.iso3:
                profile_by_iso[profile.iso3] = continent

        updated = 0
        skipped_no_run = 0
        skipped_unresolved = 0
        already_set = 0

        with transaction.atomic():
            for asset in qs.iterator():
                run = asset.pipeline_run
                if run is None:
                    skipped_no_run += 1
                    continue
                country_code = run.country_code or ""
                continent = profile_by_iso.get(country_code, "") if country_code else ""
                if not continent:
                    skipped_unresolved += 1
                    self.stdout.write(self.style.WARNING(
                        f"  asset {asset.id}: no continent for country_code={country_code!r}"
                    ))
                    continue
                if asset.continent and not force:
                    already_set += 1
                    continue
                if dry_run:
                    self.stdout.write(f"  [dry-run] asset {asset.id}: {country_code} -> {continent}")
                else:
                    asset.continent = continent
                    asset.save(update_fields=["continent"])
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. updated={updated} (dry_run={dry_run}), "
            f"skipped_no_run={skipped_no_run}, "
            f"skipped_unresolved={skipped_unresolved}, "
            f"already_set={already_set}."
        ))
